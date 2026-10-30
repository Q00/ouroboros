"""Experimental streaming Copilot runtime, selected by ``copilot_transport=acp``."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

from ouroboros.copilot.acp_client import AcpClientError, CopilotAcpClient
from ouroboros.copilot.acp_events import CopilotAcpEventTranslator
from ouroboros.copilot.acp_permissions import (
    CopilotAcpPermissions,
    resolve_acp_permission_mode,
)
from ouroboros.orchestrator.adapter import (
    AgentMessage,
    ParamSupport,
    ResolvedWorkerCwd,
    RuntimeCapabilities,
    RuntimeHandle,
    SkillDispatchHandler,
    worker_cwd_failure_message,
)
from ouroboros.orchestrator.copilot_cli_runtime import CopilotCliRuntime

_FALLBACK_ERRORS = frozenset(
    {
        "cli_unavailable",
        "malformed_response",
        "process_exited",
        "protocol_error",
        "protocol_incompatible",
        "rpc_error",
        "timeout",
    }
)


class _ReadOnlyCliFallback(CopilotCliRuntime):
    """Reuse legacy execution without weakening an ACP tool/approval envelope."""

    def __init__(
        self,
        *,
        permissions: CopilotAcpPermissions,
        verify_original_executable: Callable[[], None],
        **kwargs: Any,
    ) -> None:
        self._permissions = permissions
        self._verify_original_executable = verify_original_executable
        self._verify_original_executable()
        super().__init__(**kwargs)

    def _build_command(self, *args: Any, **kwargs: Any) -> list[str]:
        self._verify_original_executable()
        return super()._build_command(*args, **kwargs)

    def _build_permission_args(self) -> list[str]:
        return [*self._permissions.cli_args(), "--silent"]

    def _parse_json_event(self, line: str) -> dict[str, Any]:
        # The legacy command requests plain text, not --output-format=json.
        return {"type": "agent.message", "message": {"text": line}}

    def _update_last_content(self, last_content: str, message: AgentMessage) -> str:
        if message.type == "assistant" and not message.tool_name:
            return f"{last_content}\n{message.content}" if last_content else message.content
        return last_content


class CopilotAcpRuntime(CopilotCliRuntime):
    """Stream ACP activity through the same runtime and persistence contracts.

    Native sessions are deliberately not resumable in this first transport.
    The Copilot backend identity, model/profile resolution, skill interception,
    executable attestation, child environment, and Result API remain shared with
    ``CopilotCliRuntime``.
    """

    _provider_name = "copilot_acp"
    _runtime_error_type = "CopilotAcpError"
    _log_namespace = "copilot_acp_runtime"
    _display_name = "Copilot ACP"

    def __init__(
        self,
        cli_path: str | Path | None = None,
        permission_mode: str | None = None,
        model: str | None = None,
        cwd: str | Path | ResolvedWorkerCwd | None = None,
        skills_dir: str | Path | None = None,
        skill_dispatcher: SkillDispatchHandler | None = None,
        llm_backend: str | None = None,
        runtime_profile: str | None = None,
        startup_output_timeout_seconds: float | None = None,
        stdout_idle_timeout_seconds: float | None = None,
        fallback_to_cli: bool = True,
    ) -> None:
        super().__init__(
            cli_path=cli_path,
            permission_mode=permission_mode,
            model=model,
            cwd=cwd,
            skills_dir=skills_dir,
            skill_dispatcher=skill_dispatcher,
            llm_backend=llm_backend,
            runtime_profile=runtime_profile,
        )
        self._startup_output_timeout_seconds = (
            60.0 if startup_output_timeout_seconds is None else startup_output_timeout_seconds
        )
        self._stdout_idle_timeout_seconds = (
            300.0 if stdout_idle_timeout_seconds is None else stdout_idle_timeout_seconds
        )
        self._fallback_to_cli = fallback_to_cli

    @property
    def capabilities(self) -> RuntimeCapabilities:
        return replace(
            super().capabilities,
            structured_output=True,
            tool_restriction_support=ParamSupport.NATIVE,
            empty_tool_restriction_support=ParamSupport.NATIVE,
            # The runner forces bypass; ACP deliberately caps it to acceptEdits.
            permission_mode_support=ParamSupport.TRANSLATED,
        )

    def _resolve_permission_mode(self, permission_mode: str | None) -> str:
        return resolve_acp_permission_mode(permission_mode)

    def _build_permission_args(self) -> list[str]:
        # Permissions are answered via ACP, not --allow-all / --allow-all-tools.
        return []

    def _bind_acp_controls(
        self, handle: RuntimeHandle, client: CopilotAcpClient, state: dict[str, Any]
    ) -> RuntimeHandle:
        state["handle"] = handle

        async def observe(_handle: RuntimeHandle) -> dict[str, Any]:
            if client.process is not None:
                state["returncode"] = client.process.returncode
            snapshot = await self._observe_bound_runtime_handle(state)
            snapshot["can_terminate"] = state.get("returncode") is None and not client.completed
            return snapshot

        async def terminate(_handle: RuntimeHandle) -> bool:
            if client.process is None or client.process.returncode is not None or client.completed:
                return False
            state.update(runtime_status="terminating", terminated=True)
            await client.close()
            state.update(runtime_status="terminated", returncode=client.process.returncode)
            return True

        return handle.bind_controls(observe_callback=observe, terminate_callback=terminate)

    def _build_acp_command(
        self,
        permissions: CopilotAcpPermissions,
        handle: RuntimeHandle | None,
        reasoning_effort: str | None,
    ) -> list[str]:
        # Fail closed on unavailable/drifting executable evidence; this is not
        # a protocol incompatibility and must never authorize CLI fallback.
        self._verify_cli_executable_identity_unchanged()
        command = super()._build_command(
            "",
            runtime_handle=handle,
            reasoning_effort=reasoning_effort,
        )
        return [
            *command[:-2],  # Remove the legacy -p argument; ACP owns stdin.
            "--acp",
            "--stdio",
            "--no-auto-update",
            "--no-remote-export",
            "--disable-builtin-mcps",
            *permissions.cli_args(),
        ]

    async def execute_task(
        self,
        prompt: str,
        tools: list[str] | None = None,
        system_prompt: str | None = None,
        resume_handle: RuntimeHandle | None = None,
        resume_session_id: str | None = None,
        reasoning_effort: str | None = None,
        model: str | None = None,
    ) -> AsyncIterator[AgentMessage]:
        cwd_failure = worker_cwd_failure_message(
            self._cwd, runtime_backend=self._runtime_backend, resume_handle=resume_handle
        )
        if cwd_failure is not None:
            yield cwd_failure
            return
        handle = resume_handle
        intercepted = await self._maybe_dispatch_skill_intercept(prompt, handle)
        if intercepted is not None:
            for message in intercepted:
                yield message
            return
        # A capsule may carry scope/profile metadata but no native session.
        if resume_session_id or (handle is not None and handle.resume_session_id):
            yield self._error(
                "Copilot ACP native session resume is not supported; start a fresh task.",
                "UnsupportedResume",
                handle,
            )
            return
        client: CopilotAcpClient | None = None
        control_state: dict[str, Any] | None = None
        try:
            if model is not None:
                raise ValueError("Copilot ACP model overrides must be set at runtime construction")
            assert self._cwd is not None
            permissions = CopilotAcpPermissions.for_task(self._cwd, self._permission_mode, tools)
            command = await asyncio.to_thread(
                self._build_acp_command, permissions, handle, reasoning_effort
            )
            client = CopilotAcpClient(
                command,
                cwd=self._cwd,
                env=self._build_child_env(),
                permission_handler=permissions.request_permission,
                startup_timeout=self._startup_output_timeout_seconds,
                idle_timeout=self._stdout_idle_timeout_seconds,
                shutdown_timeout=self._process_shutdown_timeout_seconds,
            )
            composed_prompt = self._compose_prompt(prompt, system_prompt, tools)
        except Exception as exc:
            yield self._error(f"Failed to prepare Copilot ACP: {exc}", type(exc).__name__, handle)
            return

        try:
            async with client:
                yield AgentMessage(
                    type="system",
                    content="Connecting to Copilot ACP",
                    data={"runtime_transport": "acp", "runtime_event_type": "runtime.connected"},
                    resume_handle=handle,
                )
                await client.initialize()
                session = await client.new_session()
                session_id = session["sessionId"]
                handle = self._build_runtime_handle(session_id, handle)
                assert handle is not None and client.process is not None
                handle = replace(
                    handle,
                    # RuntimeHandle.can_resume is driven by IDs, not capabilities.
                    # Keep diagnostic ACP IDs out of its reconnect selectors.
                    native_session_id=None,
                    metadata={
                        **handle.metadata,
                        "acp_session_id": session_id,
                        "runtime_transport": "acp",
                        "runtime_event_type": "session.started",
                        "native_resume_supported": False,
                    },
                )
                control_state = {
                    "handle": handle,
                    "process_id": client.process.pid,
                    "returncode": None,
                    "runtime_status": "running",
                    "terminated": False,
                }
                handle = self._bind_acp_controls(handle, client, control_state)
                yield AgentMessage(
                    type="system",
                    content=f"Copilot ACP session started: {session_id}",
                    data={
                        "subtype": "init",
                        "session_id": session_id,
                        "acp_session_id": session_id,
                        "runtime_transport": "acp",
                        "runtime_event_type": "session.started",
                    },
                    resume_handle=handle,
                )
                translator = CopilotAcpEventTranslator(
                    session_id, empty_tool_marker=permissions.empty_tool_marker
                )
                for frame in client.startup_updates:
                    for message in translator.translate(frame, handle):
                        yield message
                client.startup_updates.clear()
                async for frame in client.prompt(composed_prompt):
                    for message in translator.translate(frame, handle):
                        if message.is_final and handle is not None:
                            handle = replace(
                                handle,
                                metadata={
                                    **handle.metadata,
                                    "runtime_event_type": (
                                        "run.failed" if message.is_error else "run.completed"
                                    ),
                                },
                            )
                            control_state["runtime_status"] = (
                                "failed" if message.is_error else "completed"
                            )
                            handle = self._bind_acp_controls(handle, client, control_state)
                            message = replace(message, resume_handle=handle)
                        yield message
        except AcpClientError as exc:
            if control_state is not None and not control_state.get("terminated"):
                control_state["runtime_status"] = "failed"
            can_fallback = (
                self._fallback_to_cli
                and not client.prompt_sent
                and exc.error_type in _FALLBACK_ERRORS
                and permissions.allows_cli_fallback
            )
            if not can_fallback:
                message = str(exc)
                if not client.prompt_sent and exc.error_type in _FALLBACK_ERRORS:
                    message += (
                        " CLI fallback was disabled or cannot preserve this tool/permission "
                        "envelope. Set copilot_transport=cli explicitly to use the legacy runtime."
                    )
                yield self._error(message, exc.error_type, handle)
                return
            try:
                fallback = await asyncio.to_thread(
                    _ReadOnlyCliFallback,
                    permissions=permissions,
                    verify_original_executable=self._verify_cli_executable_identity_unchanged,
                    cli_path=self._cli_path,
                    permission_mode=self._permission_mode,
                    model=self._model,
                    cwd=ResolvedWorkerCwd(self._cwd),
                    skills_dir=self._skills_dir,
                    skill_dispatcher=self._skill_dispatcher,
                    llm_backend=self._llm_backend,
                    runtime_profile=self._runtime_profile,
                )
            except Exception as fallback_error:
                yield self._error(
                    f"Cannot initialize Copilot CLI fallback: {fallback_error}",
                    type(fallback_error).__name__,
                    handle,
                )
                return
            yield AgentMessage(
                type="system",
                content=f"Copilot ACP unavailable; using read-only CLI fallback: {exc}",
                data={
                    "runtime_event_type": "runtime.fallback",
                    "runtime_transport": "cli",
                    "fallback_from": "acp",
                    "fallback_reason": exc.error_type,
                },
                resume_handle=resume_handle,
            )
            async for message in fallback.execute_task(
                prompt=prompt,
                tools=tools,
                system_prompt=system_prompt,
                resume_handle=resume_handle,
                reasoning_effort=reasoning_effort,
            ):
                fallback_handle = message.resume_handle
                if fallback_handle is not None:
                    fallback_handle = replace(
                        fallback_handle,
                        metadata={**fallback_handle.metadata, "runtime_transport": "cli"},
                    )
                yield replace(
                    message,
                    data={**message.data, "runtime_transport": "cli", "fallback_from": "acp"},
                    resume_handle=fallback_handle,
                )
        except Exception as exc:
            if control_state is not None and not control_state.get("terminated"):
                control_state["runtime_status"] = "failed"
            yield self._error(f"Copilot ACP failed: {exc}", type(exc).__name__, handle)

    @staticmethod
    def _error(message: str, error_type: str, handle: RuntimeHandle | None) -> AgentMessage:
        if handle is not None:
            handle = replace(
                handle, metadata={**handle.metadata, "runtime_event_type": "run.failed"}
            )
        return AgentMessage(
            type="result",
            content=message,
            data={
                "subtype": "error",
                "error_type": error_type,
                "runtime_transport": "acp",
                "runtime_event_type": "turn.failed",
            },
            resume_handle=handle,
        )


__all__ = ["CopilotAcpRuntime"]
