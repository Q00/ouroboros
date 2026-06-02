"""Pi bridge-backed agent runtime.

This runtime does not know where a developer's local ``pi`` checkout lives.
It translates the common :class:`AgentRuntime` call surface into a
JSON-serializable bridge request and delegates delivery to either an injected
fixture callable, an importable module function, or a command that speaks JSON
over stdin/stdout.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
import importlib
import inspect
import json
import os
from pathlib import Path
import shlex
from typing import Any

from ouroboros.core.errors import ProviderError
from ouroboros.core.types import Result
from ouroboros.orchestrator.adapter import (
    AgentMessage,
    RuntimeCapabilities,
    RuntimeHandle,
    SkillDispatchHandler,
    TaskResult,
)
from ouroboros.orchestrator.skill_intercept import SkillInterceptor

type PiBridge = Callable[[dict[str, Any]], dict[str, Any] | Awaitable[dict[str, Any]]]

_PI_CAPABILITIES = RuntimeCapabilities(
    skill_dispatch=True,
    targeted_resume=True,
    structured_output=True,
)
_DEFAULT_PI_BRIDGE_PACKAGE = "pi"
_DEFAULT_PI_BRIDGE_FUNCTION = "runtime.bridge:execute"
_DEFAULT_STARTUP_OUTPUT_TIMEOUT_SECONDS = 120.0
_DEFAULT_STDOUT_IDLE_TIMEOUT_SECONDS = 600.0


class PiRuntime:
    """Agent runtime that sends Ouroboros task execution to a Pi bridge."""

    _runtime_backend_name = "pi"

    def __init__(
        self,
        *,
        bridge: PiBridge | None = None,
        bridge_package: str | None = None,
        bridge_module: str | None = None,
        bridge_command: str | None = None,
        model: str | None = None,
        cwd: str | Path | None = None,
        permission_mode: str | None = None,
        llm_backend: str | None = None,
        skill_dispatcher: SkillDispatchHandler | None = None,
        skills_dir: str | Path | None = None,
        startup_output_timeout_seconds: float | None = None,
        stdout_idle_timeout_seconds: float | None = None,
        **_kwargs: object,
    ) -> None:
        self._bridge = bridge
        self._bridge_package = bridge_package or self._resolve_bridge_package()
        self._bridge_module = bridge_module or self._resolve_bridge_module()
        self._bridge_command = bridge_command or self._resolve_bridge_command()
        self._model = model
        self._cwd = str(Path(cwd).expanduser()) if cwd is not None else os.getcwd()
        self._permission_mode = permission_mode or "acceptEdits"
        self._llm_backend = llm_backend or "pi"
        self._skill_dispatcher = skill_dispatcher
        self._startup_output_timeout_seconds = (
            _DEFAULT_STARTUP_OUTPUT_TIMEOUT_SECONDS
            if startup_output_timeout_seconds is None
            else float(startup_output_timeout_seconds)
        )
        self._stdout_idle_timeout_seconds = (
            _DEFAULT_STDOUT_IDLE_TIMEOUT_SECONDS
            if stdout_idle_timeout_seconds is None
            else float(stdout_idle_timeout_seconds)
        )
        self._skill_interceptor = SkillInterceptor(
            cwd=self._cwd,
            runtime_backend=self._runtime_backend_name,
            runtime_handle_backend=self._runtime_backend_name,
            permission_mode=self._permission_mode,
            llm_backend=self._llm_backend,
            log_namespace="pi_runtime",
            skills_dir=skills_dir,
            skill_dispatcher=skill_dispatcher,
        )

    @property
    def runtime_backend(self) -> str:
        return self._runtime_backend_name

    @property
    def capabilities(self) -> RuntimeCapabilities:
        return _PI_CAPABILITIES

    @property
    def llm_backend(self) -> str | None:
        return self._llm_backend

    @property
    def working_directory(self) -> str | None:
        return self._cwd

    @property
    def permission_mode(self) -> str | None:
        return self._permission_mode

    @property
    def bridge_module(self) -> str | None:
        return self._bridge_module

    @property
    def bridge_command(self) -> str | None:
        return self._bridge_command

    @property
    def startup_output_timeout_seconds(self) -> float:
        return self._startup_output_timeout_seconds

    @property
    def stdout_idle_timeout_seconds(self) -> float:
        return self._stdout_idle_timeout_seconds

    async def execute_task(
        self,
        prompt: str,
        tools: list[str] | None = None,
        system_prompt: str | None = None,
        resume_handle: RuntimeHandle | None = None,
        resume_session_id: str | None = None,
    ) -> AsyncIterator[AgentMessage]:
        current_handle = resume_handle
        intercepted_messages = await self._skill_interceptor.maybe_dispatch(prompt, current_handle)
        if intercepted_messages is not None:
            for message in intercepted_messages:
                yield message
            return

        request = self._build_bridge_request(
            prompt=prompt,
            tools=tools,
            system_prompt=system_prompt,
            resume_handle=resume_handle,
            resume_session_id=resume_session_id,
        )
        yield AgentMessage(
            type="system",
            content="Starting Pi bridge runtime",
            data={"subtype": "init", "runtime_config_source": request["runtime_config_source"]},
            resume_handle=current_handle,
        )
        try:
            response = await self._invoke_bridge(request)
        except Exception as exc:
            yield AgentMessage(
                type="result",
                content=f"Pi bridge failed: {exc}",
                data={"subtype": "error", "error_type": type(exc).__name__},
                resume_handle=current_handle,
            )
            return

        current_handle = self._build_runtime_handle(response, fallback=current_handle)
        for message in self._messages_from_response(response, current_handle):
            yield message

    async def execute_task_to_result(
        self,
        prompt: str,
        tools: list[str] | None = None,
        system_prompt: str | None = None,
        resume_handle: RuntimeHandle | None = None,
        resume_session_id: str | None = None,
    ) -> Result[TaskResult, ProviderError]:
        messages: list[AgentMessage] = []
        async for message in self.execute_task(
            prompt=prompt,
            tools=tools,
            system_prompt=system_prompt,
            resume_handle=resume_handle,
            resume_session_id=resume_session_id,
        ):
            messages.append(message)

        if not messages:
            return Result.err(ProviderError("No messages from Pi bridge"))

        final = messages[-1]
        if final.is_final and not final.is_error:
            return Result.ok(
                TaskResult(
                    success=True,
                    final_message=final.content,
                    messages=tuple(messages),
                    session_id=final.resume_handle.native_session_id
                    if final.resume_handle is not None
                    else None,
                    resume_handle=final.resume_handle,
                )
            )
        return Result.err(ProviderError(final.content))

    def _build_bridge_request(
        self,
        *,
        prompt: str,
        tools: list[str] | None,
        system_prompt: str | None,
        resume_handle: RuntimeHandle | None,
        resume_session_id: str | None,
    ) -> dict[str, Any]:
        effective_resume_session_id = resume_session_id
        if effective_resume_session_id is None and resume_handle is not None:
            effective_resume_session_id = resume_handle.resume_session_id

        session_metadata: dict[str, Any] = {}
        if resume_handle is not None:
            session_metadata.update(resume_handle.metadata)
        if effective_resume_session_id is not None:
            session_metadata["resume_session_id"] = effective_resume_session_id

        return {
            "prompt": prompt,
            "system_prompt": system_prompt,
            "cwd": self._cwd,
            "permission_mode": self._permission_mode,
            "llm_backend": self._llm_backend,
            "model_config": {"model": self._model} if self._model else {},
            "tool_policy": {
                "allowed_tools": list(tools) if tools is not None else None,
                "mode": "explicit_allowlist" if tools is not None else "runtime_default",
            },
            "resume_handle": _serialize_resume_handle(resume_handle),
            "session_metadata": session_metadata,
            "runtime_config_source": self._runtime_config_source(),
        }

    async def _invoke_bridge(self, request: dict[str, Any]) -> dict[str, Any]:
        bridge = self._bridge
        if bridge is not None:
            result = bridge(request)
            if inspect.isawaitable(result):
                result = await result
            return _ensure_mapping(result)

        if self._bridge_command:
            return await self._invoke_command_bridge(request, self._bridge_command)

        if self._bridge_module:
            function = _load_bridge_function(self._bridge_module)
            result = function(request)
            if inspect.isawaitable(result):
                result = await result
            return _ensure_mapping(result)

        msg = "Pi bridge is not configured"
        raise RuntimeError(msg)

    async def _invoke_command_bridge(
        self, request: dict[str, Any], bridge_command: str
    ) -> dict[str, Any]:
        args = shlex.split(bridge_command)
        if not args:
            msg = "Pi bridge command is empty"
            raise ValueError(msg)
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=self._cwd,
        )
        input_bytes = json.dumps(request, sort_keys=True).encode("utf-8")
        communicate = proc.communicate(input_bytes)
        timeout_seconds = self._command_bridge_timeout_seconds()
        if timeout_seconds is None:
            stdout, stderr = await communicate
        else:
            try:
                stdout, stderr = await asyncio.wait_for(communicate, timeout=timeout_seconds)
            except TimeoutError as exc:
                proc.kill()
                await proc.wait()
                msg = f"Pi bridge command timed out after {timeout_seconds:g}s without completing"
                raise TimeoutError(msg) from exc
        if proc.returncode != 0:
            detail = stderr.decode(errors="replace").strip()
            msg = f"Pi bridge command failed (exit {proc.returncode}): {detail}"
            raise RuntimeError(msg)
        return _ensure_mapping(json.loads(stdout.decode("utf-8")))

    def _command_bridge_timeout_seconds(self) -> float | None:
        if self._stdout_idle_timeout_seconds <= 0:
            return None
        return self._stdout_idle_timeout_seconds

    def _runtime_config_source(self) -> dict[str, str]:
        if self._bridge is not None:
            return {"kind": "fixture", "value": "callable"}
        if self._bridge_command:
            return {"kind": "command", "value": self._bridge_command}
        return {"kind": "module", "value": self._bridge_module or ""}

    def _build_runtime_handle(
        self,
        response: dict[str, Any],
        *,
        fallback: RuntimeHandle | None,
    ) -> RuntimeHandle:
        metadata = dict(fallback.metadata) if fallback is not None else {}
        response_metadata = response.get("session_metadata")
        if isinstance(response_metadata, dict):
            metadata.update(response_metadata)

        native_session_id = _optional_str(response.get("session_id"))
        if native_session_id is None and fallback is not None:
            native_session_id = fallback.native_session_id

        return RuntimeHandle(
            backend=self._runtime_backend_name,
            native_session_id=native_session_id,
            conversation_id=_optional_str(response.get("conversation_id"))
            or (fallback.conversation_id if fallback is not None else None),
            previous_response_id=_optional_str(response.get("previous_response_id")),
            cwd=self._cwd,
            approval_mode=self._permission_mode,
            metadata=metadata,
        )

    def _messages_from_response(
        self, response: dict[str, Any], handle: RuntimeHandle
    ) -> tuple[AgentMessage, ...]:
        messages: list[AgentMessage] = []
        raw_messages = response.get("messages")
        if isinstance(raw_messages, list):
            for raw in raw_messages:
                if not isinstance(raw, dict):
                    continue
                messages.append(
                    AgentMessage(
                        type=str(raw.get("type", "assistant")),
                        content=str(raw.get("content", "")),
                        tool_name=_optional_str(raw.get("tool_name")),
                        data=raw.get("data") if isinstance(raw.get("data"), dict) else {},
                        resume_handle=handle,
                    )
                )

        final_content = _optional_str(response.get("final_message")) or _optional_str(
            response.get("content")
        )
        if final_content and not messages:
            messages.append(
                AgentMessage(
                    type="assistant",
                    content=final_content,
                    resume_handle=handle,
                )
            )

        if response.get("success", True):
            result_content = final_content or "\n".join(
                message.content for message in messages if message.content
            )
            messages.append(
                AgentMessage(
                    type="result",
                    content=result_content,
                    data={"subtype": "success"},
                    resume_handle=handle,
                )
            )
        else:
            messages.append(
                AgentMessage(
                    type="result",
                    content=final_content
                    or _optional_str(response.get("error"))
                    or "Pi bridge failed",
                    data={"subtype": "error", "error": response.get("error")},
                    resume_handle=handle,
                )
            )
        return tuple(messages)

    def _resolve_bridge_package(self) -> str:
        env_value = os.environ.get("OUROBOROS_PI_BRIDGE_PACKAGE", "").strip()
        if env_value:
            return env_value
        try:
            from ouroboros.config import get_pi_bridge_package

            configured = get_pi_bridge_package()
        except Exception:
            configured = None
        return configured or _DEFAULT_PI_BRIDGE_PACKAGE

    def _resolve_bridge_module(self) -> str | None:
        env_value = os.environ.get("OUROBOROS_PI_BRIDGE_MODULE", "").strip()
        if env_value:
            return env_value
        try:
            from ouroboros.config import get_pi_bridge_module

            configured = get_pi_bridge_module()
        except Exception:
            configured = None
        if configured:
            return configured
        return f"{self._bridge_package}.{_DEFAULT_PI_BRIDGE_FUNCTION}"

    def _resolve_bridge_command(self) -> str | None:
        env_value = os.environ.get("OUROBOROS_PI_BRIDGE_COMMAND", "").strip()
        if env_value:
            return env_value
        try:
            from ouroboros.config import get_pi_bridge_command

            return get_pi_bridge_command()
        except Exception:
            return None


def _load_bridge_function(spec: str) -> PiBridge:
    module_name, sep, attr_name = spec.partition(":")
    if not sep or not module_name or not attr_name:
        msg = "Pi bridge module must use 'module:function' format"
        raise ValueError(msg)
    module = importlib.import_module(module_name)
    function = getattr(module, attr_name)
    if not callable(function):
        msg = f"Pi bridge target is not callable: {spec}"
        raise TypeError(msg)
    return function


def _ensure_mapping(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        msg = "Pi bridge response must be a mapping"
        raise TypeError(msg)
    return value


def _serialize_resume_handle(handle: RuntimeHandle | None) -> dict[str, Any] | None:
    if handle is None:
        return None
    return {
        "backend": handle.backend,
        "kind": handle.kind,
        "native_session_id": handle.native_session_id,
        "conversation_id": handle.conversation_id,
        "previous_response_id": handle.previous_response_id,
        "transcript_path": handle.transcript_path,
        "cwd": handle.cwd,
        "approval_mode": handle.approval_mode,
        "updated_at": handle.updated_at,
        "metadata": dict(handle.metadata),
    }


def _optional_str(value: object) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text or None


__all__ = ["PiBridge", "PiRuntime"]
