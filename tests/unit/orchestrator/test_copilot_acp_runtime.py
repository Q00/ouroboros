"""Offline process-to-runtime tests for Copilot's ACP transport."""

from __future__ import annotations

import asyncio
from contextlib import aclosing
import json
from pathlib import Path

import pytest

from ouroboros.copilot.acp_client import AcpClientError, CopilotAcpClient
from ouroboros.copilot.acp_permissions import CopilotAcpPermissions
from ouroboros.orchestrator.adapter import ParamSupport, RuntimeHandle
from ouroboros.orchestrator.copilot_acp_runtime import CopilotAcpRuntime
from ouroboros.orchestrator.runtime_message_projection import project_runtime_message

_FAKE = Path(__file__).parents[2] / "fixtures" / "fake_copilot_acp.py"


@pytest.fixture
def log_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "wire.jsonl"
    monkeypatch.setenv("FAKE_COPILOT_ACP_LOG", str(path))
    monkeypatch.setenv("FAKE_COPILOT_ACP_MODE", "normal")
    monkeypatch.setattr(CopilotAcpRuntime, "_process_shutdown_timeout_seconds", 0.1)
    return path


def records(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def runtime(tmp_path: Path, **kwargs: object) -> CopilotAcpRuntime:
    return CopilotAcpRuntime(cli_path=_FAKE, cwd=tmp_path, permission_mode="acceptEdits", **kwargs)


async def collect(adapter: CopilotAcpRuntime, tools: list[str] | None = None) -> list:
    return [message async for message in adapter.execute_task("Inspect and test", tools=tools)]


async def test_live_tool_event_is_observed_before_prompt_finishes(
    tmp_path: Path, log_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    gate = tmp_path / "observed"
    monkeypatch.setenv("FAKE_COPILOT_ACP_GATE", str(gate))
    messages = []
    async for message in runtime(tmp_path).execute_task("Work", tools=["Bash"]):
        messages.append(message)
        if project_runtime_message(message).is_tool_call:
            assert not any(row.get("prompt_completed") for row in records(log_path))
            gate.touch()
    assert gate.exists()
    assert sum(message.is_final for message in messages) == 1
    assert messages[-1].content == "All done.\n"
    assert not messages[-1].is_error
    assert messages[-1].data["usage"]["total_tokens"] == 17
    assert not any("PRIVATE_REASONING" in message.content for message in messages)
    permission_reply = next(row for row in records(log_path) if row.get("id") == 0)
    assert permission_reply["result"]["outcome"]["optionId"] == "once"
    assert messages[-1].resume_handle.metadata["runtime_transport"] == "acp"


@pytest.mark.parametrize(
    "mode",
    [
        "unsupported",
        "malformed",
        "bad_envelope",
        "bad_version",
        "missing_session",
        "session_failure",
    ],
)
async def test_pre_prompt_failures_can_use_read_only_legacy_fallback(
    tmp_path: Path, log_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    monkeypatch.setenv("FAKE_COPILOT_ACP_MODE", mode)
    messages = await collect(runtime(tmp_path), tools=["Read"])
    assert messages[-1].content == "Legacy fallback answer\nSecond line"
    assert not messages[-1].is_error
    assert any(m.data.get("fallback_from") == "acp" for m in messages)
    assert messages[-1].data["runtime_transport"] == "cli"
    launches = [row["argv"] for row in records(log_path) if "argv" in row]
    assert len(launches) == 2
    assert "--available-tools=view" in launches[-1]
    assert "--allow-all" not in launches[-1] and "--allow-all-tools" not in launches[-1]


@pytest.mark.parametrize(
    "mode",
    [
        "auth_init",
        "auth_session",
        "auth_prompt",
        "stderr_auth",
        "auth_data",
        "auth_plain",
        "auth_unauthenticated",
        "auth_truncated",
        "auth_nonrpc",
    ],
)
async def test_authentication_is_surfaced_without_fallback(
    tmp_path: Path, log_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    monkeypatch.setenv("FAKE_COPILOT_ACP_MODE", mode)
    messages = await collect(runtime(tmp_path), tools=[])
    assert messages[-1].is_error
    assert messages[-1].data["error_type"] == "authentication_error"
    assert len([r for r in records(log_path) if "argv" in r]) == 1
    assert not any(m.data.get("fallback_from") for m in messages)


@pytest.mark.parametrize(
    "mode", ["malformed_prompt", "exit_prompt", "missing_stop", "oversized", "malformed_status"]
)
async def test_never_replays_a_prompt_after_submission(
    tmp_path: Path, log_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    monkeypatch.setenv("FAKE_COPILOT_ACP_MODE", mode)
    messages = await collect(runtime(tmp_path), tools=[])
    assert messages[-1].is_error
    assert sum(m.is_final for m in messages) == 1
    assert len([r for r in records(log_path) if "argv" in r]) == 1


async def test_mutating_envelope_fails_closed_instead_of_losing_permissions(
    tmp_path: Path, log_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_COPILOT_ACP_MODE", "unsupported")
    messages = await collect(runtime(tmp_path), tools=["Edit"])
    assert messages[-1].is_error
    assert "cannot preserve" in messages[-1].content
    assert len([r for r in records(log_path) if "argv" in r]) == 1


async def test_fallback_can_be_disabled(
    tmp_path: Path, log_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_COPILOT_ACP_MODE", "unsupported")
    messages = await collect(runtime(tmp_path, fallback_to_cli=False), tools=[])
    assert messages[-1].is_error
    assert len([r for r in records(log_path) if "argv" in r]) == 1


@pytest.mark.parametrize("mode", ["foreign_permission", "unknown_request"])
async def test_server_requests_are_answered_fail_closed_without_hanging(
    tmp_path: Path, log_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    monkeypatch.setenv("FAKE_COPILOT_ACP_MODE", mode)
    messages = await collect(runtime(tmp_path), tools=["Bash"])
    assert not messages[-1].is_error
    reply = next(row for row in records(log_path) if row.get("id") == 0)
    if mode == "foreign_permission":
        assert reply["result"]["outcome"]["outcome"] == "cancelled"
    else:
        assert reply["error"]["code"] == -32601


async def test_large_stderr_is_drained_independently(
    tmp_path: Path, log_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_COPILOT_ACP_MODE", "stderr_flood")
    messages = await collect(runtime(tmp_path), tools=["Bash"])
    assert not messages[-1].is_error


async def test_consumer_close_cancels_native_turn_and_reaps_process(
    tmp_path: Path, log_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_COPILOT_ACP_MODE", "hang_prompt")
    adapter = runtime(tmp_path)
    async with aclosing(adapter.execute_task("Work", tools=["Read"])) as stream:
        async for message in stream:
            if message.type == "assistant":
                break
    assert any(row.get("cancel_observed") for row in records(log_path))


async def test_async_cancellation_is_not_converted_to_fallback(
    tmp_path: Path, log_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_COPILOT_ACP_MODE", "hang_prompt")
    adapter = runtime(tmp_path)
    got_chunk = asyncio.Event()

    async def run() -> None:
        async for message in adapter.execute_task("Work", tools=["Read"]):
            if message.type == "assistant":
                got_chunk.set()

    task = asyncio.create_task(run())
    await asyncio.wait_for(got_chunk.wait(), 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert any(row.get("cancel_observed") for row in records(log_path))
    assert len([r for r in records(log_path) if "argv" in r]) == 1


async def test_runtime_handle_termination_sends_acp_cancel(
    tmp_path: Path, log_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_COPILOT_ACP_MODE", "hang_prompt")
    adapter = runtime(tmp_path)
    messages = []
    async for message in adapter.execute_task("Work", tools=["Read"]):
        messages.append(message)
        if message.type == "assistant":
            handle = message.resume_handle
            assert await handle.terminate()
            assert not await handle.terminate()
            snapshot = await handle.observe()
            assert snapshot["returncode"] is not None
            assert snapshot["can_terminate"] is False
    assert messages[-1].is_error
    assert any(row.get("cancel_observed") for row in records(log_path))


async def test_idle_timeout_ends_with_one_error(
    tmp_path: Path, log_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_COPILOT_ACP_MODE", "hang_prompt")
    messages = await collect(runtime(tmp_path, stdout_idle_timeout_seconds=0.2))
    assert messages[-1].is_error
    assert messages[-1].data["error_type"] == "timeout"
    assert any(row.get("cancel_observed") for row in records(log_path))


async def test_concurrent_invocations_keep_session_and_tool_state_isolated(
    tmp_path: Path, log_path: Path
) -> None:
    adapter = runtime(tmp_path)
    first, second = await asyncio.gather(collect(adapter), collect(adapter))
    assert (
        first[-1].resume_handle.metadata["acp_session_id"]
        != second[-1].resume_handle.metadata["acp_session_id"]
    )
    for messages in (first, second):
        assert sum(project_runtime_message(m).is_tool_call for m in messages) == 1
        assert messages[-1].content == "All done.\n"


async def test_native_resume_is_not_silently_replaced_by_a_new_session(
    tmp_path: Path, log_path: Path
) -> None:
    adapter = runtime(tmp_path)
    handle = RuntimeHandle(backend="copilot_cli", native_session_id="old")
    messages = [m async for m in adapter.execute_task("continue", resume_handle=handle)]
    assert len(messages) == 1 and messages[0].is_error
    assert messages[0].data["error_type"] == "UnsupportedResume"
    assert not log_path.exists()


def test_capabilities_are_honest_about_streaming_and_permissions(tmp_path: Path) -> None:
    adapter = runtime(tmp_path)
    assert adapter.capabilities.structured_output
    assert not adapter.capabilities.targeted_resume
    assert adapter.capabilities.tool_restriction_support is ParamSupport.NATIVE
    assert adapter.capabilities.empty_tool_restriction_support is ParamSupport.NATIVE
    assert adapter.capabilities.permission_mode_support is ParamSupport.TRANSLATED
    assert adapter._resolve_permission_mode("bypassPermissions") == "acceptEdits"


async def test_client_startup_timeout_is_bounded_and_process_reaped(
    tmp_path: Path, log_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import os

    monkeypatch.setenv("FAKE_COPILOT_ACP_MODE", "hang_init")
    permissions = CopilotAcpPermissions.for_task(str(tmp_path), "default", [])
    client = CopilotAcpClient(
        [str(_FAKE)],
        cwd=str(tmp_path),
        env=os.environ.copy(),
        permission_handler=permissions.request_permission,
        startup_timeout=0.1,
        shutdown_timeout=0.1,
    )
    with pytest.raises(AcpClientError, match="timed out"):
        async with client:
            await client.initialize()
    assert client.process.returncode is not None


@pytest.mark.parametrize("mode", ["normal", "cancelled", "auth_prompt"])
async def test_shared_task_result_api_preserves_success_and_errors(
    tmp_path: Path, log_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    monkeypatch.setenv("FAKE_COPILOT_ACP_MODE", mode)
    result = await runtime(tmp_path).execute_task_to_result("Work", tools=["Bash"])
    assert result.is_ok is (mode == "normal")
    if result.is_ok:
        assert result.value.final_message == "All done.\n"
        assert result.value.session_id is None
        assert result.value.resume_handle.metadata["acp_session_id"]
        assert result.value.resume_handle.metadata["runtime_transport"] == "acp"
    else:
        assert result.error.provider == "copilot_acp"


async def test_no_tools_is_enforced_in_the_actual_child_command(
    tmp_path: Path, log_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_COPILOT_ACP_MODE", "no_tools")
    messages = await collect(runtime(tmp_path), tools=[])
    assert not messages[-1].is_error
    assert not any(project_runtime_message(m).is_tool_call for m in messages)
    assert messages[-1].content == "No tools used."
    assert sum(m.data.get("runtime_event_type") == "runtime.info" for m in messages) == 2


async def test_failed_executable_attestation_never_authorizes_fallback(
    tmp_path: Path, log_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = runtime(tmp_path)

    def fail() -> None:
        raise RuntimeError("No positive executable attestation")

    monkeypatch.setattr(adapter, "_verify_cli_executable_identity_unchanged", fail)
    messages = await collect(adapter, tools=[])
    assert messages[-1].is_error
    assert "attestation" in messages[-1].content
    assert not log_path.exists()


async def test_fallback_does_not_reauthorize_changed_original_executable(
    tmp_path: Path, log_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_COPILOT_ACP_MODE", "unsupported")
    adapter = runtime(tmp_path)
    verify = adapter._verify_cli_executable_identity_unchanged

    def verify_original() -> None:
        if log_path.exists():
            raise RuntimeError("Original executable changed after ACP startup")
        verify()

    monkeypatch.setattr(adapter, "_verify_cli_executable_identity_unchanged", verify_original)
    messages = await collect(adapter, tools=[])
    assert messages[-1].is_error
    assert "Original executable changed" in messages[-1].content
    assert len([r for r in records(log_path) if "argv" in r]) == 1


def test_acp_command_reuses_model_effort_and_profile_selection(tmp_path: Path) -> None:
    adapter = runtime(tmp_path, model="claude-haiku-4.5")
    permissions = CopilotAcpPermissions.for_task(str(tmp_path), "acceptEdits", ["Read"])
    command = adapter._build_acp_command(permissions, None, "high")
    assert command[command.index("--model") + 1] == "claude-haiku-4.5"
    assert command[command.index("--reasoning-effort") + 1] == "high"
    assert "--acp" in command and "--stdio" in command and "-p" not in command
    assert "--available-tools=view" in command
    adapter._copilot_agent = "reviewer"
    command = adapter._build_acp_command(permissions, None, "unknown-effort")
    assert command[command.index("--agent") + 1] == "reviewer"
    assert "--model" not in command and "--reasoning-effort" not in command


def test_authentication_environment_is_preserved_without_copying_into_arguments(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "test-only-placeholder")
    monkeypatch.setenv("COPILOT_ALLOW_ALL", "1")
    adapter = runtime(tmp_path)
    assert adapter._build_child_env()["GITHUB_TOKEN"] == "test-only-placeholder"
    assert "COPILOT_ALLOW_ALL" not in adapter._build_child_env()


async def test_diagnostic_handle_does_not_claim_native_resume_on_later_dispatch(
    tmp_path: Path, log_path: Path
) -> None:
    from ouroboros.orchestrator.ac_runtime_handle_manager import ACRuntimeHandleManager

    adapter = runtime(tmp_path)
    first = await collect(adapter, tools=["Bash"])
    handle = first[-1].resume_handle
    assert handle.native_session_id is None
    assert handle.resume_session_id is None
    assert not handle.can_resume
    assert not ACRuntimeHandleManager._is_resumable_runtime_handle(handle)
    second = [
        message
        async for message in adapter.execute_task(
            "A new independently scoped task", tools=["Bash"], resume_handle=handle
        )
    ]
    assert not second[-1].is_error
    assert second[-1].resume_handle.metadata["acp_session_id"] != handle.metadata["acp_session_id"]


async def test_startup_notifications_preserve_acp_transport(
    tmp_path: Path, log_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_COPILOT_ACP_MODE", "startup_plan")
    messages = await collect(runtime(tmp_path), tools=["Bash"])
    plan = next(m for m in messages if m.data.get("runtime_event_type") == "agent.plan")
    assert plan.data["runtime_transport"] == "acp"
    assert plan.resume_handle.metadata["runtime_transport"] == "acp"


async def test_fallback_constructor_failure_is_a_normalized_error(
    tmp_path: Path, log_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ouroboros.orchestrator import copilot_acp_runtime

    monkeypatch.setenv("FAKE_COPILOT_ACP_MODE", "unsupported")

    def fail(**kwargs: object) -> None:
        raise RuntimeError("CLI disappeared during fallback construction")

    monkeypatch.setattr(copilot_acp_runtime, "_ReadOnlyCliFallback", fail)
    messages = await collect(runtime(tmp_path), tools=[])
    assert messages[-1].is_error
    assert "CLI disappeared" in messages[-1].content
    assert len([r for r in records(log_path) if "argv" in r]) == 1


async def test_permission_audit_ignores_server_supplied_client_fields(
    tmp_path: Path, log_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_COPILOT_ACP_MODE", "spoof_permission_audit")
    messages = await collect(runtime(tmp_path), tools=[])
    audit = next(m for m in messages if m.data.get("subtype") == "permission_resolved")
    assert audit.data["permission_approved"] is False
