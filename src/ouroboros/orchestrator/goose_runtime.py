"""Goose CLI runtime for Ouroboros orchestrator execution.

This runtime shells out to the goose CLI's headless ``goose run`` command and
normalizes its structured stream output into Ouroboros ``AgentMessage`` events.
The implementation intentionally mirrors the subprocess/runtime-handle contract
used by existing CLI runtimes while keeping Goose-specific parsing permissive:
Goose's ``stream-json`` output is treated as an evolving integration boundary,
so unknown events are ignored or surfaced as text rather than failing the run.
"""

from __future__ import annotations

from collections.abc import Mapping
import os
from pathlib import Path
import shlex
import shutil
from typing import Any
from uuid import uuid4

from ouroboros.config import get_goose_cli_path
from ouroboros.observability.logging import get_logger
from ouroboros.orchestrator.adapter import AgentMessage, RuntimeCapabilities, RuntimeHandle
from ouroboros.orchestrator.codex_cli_runtime import CodexCliRuntime

log = get_logger(__name__)


class GooseCliRuntime(CodexCliRuntime):
    """Agent runtime that executes tasks with the locally installed goose CLI.

    Goose provides a headless automation surface via ``goose run`` with
    ``--output-format stream-json``.  Ouroboros uses that command as the process
    runtime and maps emitted JSON events onto the shared runtime protocol.
    """

    _runtime_handle_backend = "goose"
    _runtime_backend = "goose"
    _requires_memory_gate = False
    _provider_name = "goose_cli"
    _runtime_error_type = "GooseCliError"
    _log_namespace = "goose_cli_runtime"
    _display_name = "Goose CLI"
    _default_cli_name = "goose"
    _default_llm_backend = "goose"
    _tempfile_prefix = "ouroboros-goose-"
    _skills_package_uri = "packaged://ouroboros.goose/skills"
    _max_turns = 1000

    def __init__(
        self,
        cli_path: str | Path | None = None,
        permission_mode: str | None = None,
        model: str | None = None,
        cwd: str | Path | None = None,
        skills_dir: str | Path | None = None,
        skill_dispatcher: Any | None = None,
        llm_backend: str | None = None,
        runtime_profile: str | None = None,
        startup_output_timeout_seconds: float | None = None,
        stdout_idle_timeout_seconds: float | None = None,
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
        if startup_output_timeout_seconds is not None:
            self._startup_output_timeout_seconds = startup_output_timeout_seconds
        if stdout_idle_timeout_seconds is not None:
            self._stdout_idle_timeout_seconds = stdout_idle_timeout_seconds

    @property
    def capabilities(self) -> RuntimeCapabilities:
        """Declare the current Goose integration feature contract.

        ``goose run`` exposes structured streaming output and named session
        resume.  The runtime uses Ouroboros-generated session names as stable
        resume handles so parallel AC workers can reconnect deterministically.
        """
        return RuntimeCapabilities(
            skill_dispatch=True,
            targeted_resume=True,
            structured_output=True,
        )

    def _resolve_permission_mode(self, permission_mode: str | None) -> str:
        """Normalize Goose permission mode values.

        Goose accepts permission behavior through ``GOOSE_MODE``.  Ouroboros
        historically uses Claude/Codex-style names, so map the common values to
        Goose's documented modes while preserving explicit Goose-native values.
        """
        candidate = (permission_mode or "auto").strip()
        aliases = {
            "default": "auto",
            "acceptedits": "auto",
            "accept_edits": "auto",
            "bypasspermissions": "auto",
            "bypass_permissions": "auto",
            "auto": "auto",
            "approve": "approve",
            "chat": "chat",
            "smart_approve": "smart_approve",
        }
        return aliases.get(candidate.lower(), candidate)

    def _build_permission_args(self) -> list[str]:
        """Goose permission mode is provided through ``GOOSE_MODE`` env."""
        return []

    def _get_configured_cli_path(self) -> str | None:
        """Resolve an explicit Goose CLI path from config helpers."""
        return get_goose_cli_path()

    def _resolve_cli_path(self, cli_path: str | Path | None) -> str:
        """Resolve the Goose CLI path from explicit, config, or PATH values."""
        candidate = cli_path or self._get_configured_cli_path()
        if candidate:
            return str(Path(candidate).expanduser())
        return shutil.which(self._default_cli_name) or self._default_cli_name

    def _normalize_model(self, model: str | None) -> str | None:
        candidate = super()._normalize_model(model)
        if candidate in {"current", "default"}:
            return None
        return candidate

    def _build_command(
        self,
        output_last_message_path: str,
        *,
        resume_session_id: str | None = None,
        prompt: str | None = None,
        runtime_handle: RuntimeHandle | None = None,
    ) -> list[str]:
        """Build ``goose run`` args. Prompt is fed through stdin.

        ``output_last_message_path`` is part of the shared CLI runtime hook but
        Goose does not have an equivalent flag; the base completion path falls
        back to the last streamed assistant/result content.
        """
        del output_last_message_path, prompt, runtime_handle

        session_name = resume_session_id or f"ouroboros-{uuid4().hex[:12]}"
        command = [
            self._cli_path,
            "run",
            "--output-format",
            "stream-json",
            "--max-turns",
            str(self._max_turns),
            "-n",
            session_name,
            "-i",
            "-",
        ]
        if resume_session_id:
            command.insert(-2, "--resume")

        normalized_model = self._normalize_model(self._model)
        if normalized_model:
            command.extend(["--model", normalized_model])

        return command

    def _resolve_resume_session_id(
        self,
        current_handle: RuntimeHandle | None,
    ) -> str | None:
        if current_handle is None:
            return None
        return current_handle.native_session_id or current_handle.conversation_id

    def _build_child_env(self) -> dict[str, str]:
        """Build an isolated environment for child Goose runtime processes."""
        env = dict(os.environ)
        env.pop("OUROBOROS_AGENT_RUNTIME", None)
        env.pop("OUROBOROS_LLM_BACKEND", None)
        env.pop("OUROBOROS_MCP_BRIDGE", None)
        env.pop("OUROBOROS_MCP_BRIDGE_CONFIG", None)
        env["_OUROBOROS_NESTED"] = "1"
        env["GOOSE_MODE"] = self._permission_mode
        if self._cwd:
            env["GOOSE_WORKING_DIR"] = self._cwd
        # Do not translate Ouroboros LLM backend names into GOOSE_PROVIDER.
        # Runtime and LLM backend names live in different namespaces, and
        # create_agent_runtime() always passes the configured Ouroboros LLM
        # backend through.  Let Goose's own config / GOOSE_PROVIDER / --provider
        # selection remain authoritative instead of injecting invalid values such
        # as "claude_code" into child Goose processes.
        return env

    def _extract_event_session_id(self, event: Mapping[str, Any]) -> str | None:
        """Extract only Goose session identifiers from stream events.

        Goose stream events can also contain generic top-level ``id`` and
        ``name`` fields for messages, tools, or event payloads.  Treating those
        as session ids corrupts targeted resume handles, so only accept
        session-specific keys at the top level and within explicit ``session``
        objects.
        """
        for key in (
            "session_id",
            "sessionId",
            "native_session_id",
            "conversation_id",
            "conversationId",
        ):
            value = event.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()

        session = event.get("session")
        if isinstance(session, Mapping):
            for key in ("session_id", "sessionId", "id", "name"):
                value = session.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
        return None

    def _extract_event_type(self, event: Mapping[str, Any]) -> str:
        for key in ("type", "event", "event_type", "kind"):
            value = event.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip().lower()
        return ""

    def _extract_tool_name_from_event(self, event: Mapping[str, Any]) -> str | None:
        for key in ("tool_name", "toolName", "name", "tool"):
            value = event.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        tool = event.get("tool")
        if isinstance(tool, Mapping):
            for key in ("name", "id"):
                value = tool.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
        return None

    def _extract_tool_input_from_event(self, event: Mapping[str, Any]) -> dict[str, Any]:
        for key in ("input", "arguments", "args", "parameters", "params"):
            value = event.get(key)
            if isinstance(value, dict):
                return dict(value)
        tool = event.get("tool")
        if isinstance(tool, Mapping):
            for key in ("input", "arguments", "args"):
                value = tool.get(key)
                if isinstance(value, dict):
                    return dict(value)
        return {}

    def _extract_text(self, value: object) -> str:
        text = super()._extract_text(value)
        if text:
            return text
        if isinstance(value, dict):
            for key in ("response", "result", "output", "data", "error"):
                if key in value:
                    text = super()._extract_text(value[key])
                    if text:
                        return text
        return ""

    def _convert_event(
        self,
        event: dict[str, Any],
        current_handle: RuntimeHandle | None,
    ) -> list[AgentMessage]:
        """Convert Goose stream-json events into normalized messages."""
        event_type = self._extract_event_type(event)
        session_id = self._extract_event_session_id(event)
        event_handle = (
            self._build_runtime_handle(session_id, current_handle) if session_id else current_handle
        )

        if event_type in {"session.created", "session.started", "session", "start", "started"}:
            if event_handle is None:
                return []
            return [
                AgentMessage(
                    type="system",
                    content=f"Session initialized: {event_handle.native_session_id}",
                    data={"subtype": "init", "session_id": event_handle.native_session_id},
                    resume_handle=event_handle,
                )
            ]

        if "tool" in event_type and not any(
            marker in event_type for marker in ("result", "output", "completed", "finish")
        ):
            tool_name = self._extract_tool_name_from_event(event) or "tool"
            tool_input = self._extract_tool_input_from_event(event)
            detail = ""
            if tool_name.lower() in {"bash", "shell", "developer.shell"}:
                command = tool_input.get("command") or tool_input.get("cmd")
                if isinstance(command, list):
                    detail = shlex.join(str(part) for part in command)
                elif isinstance(command, str):
                    detail = command
            return [
                self._build_tool_message(
                    tool_name=tool_name,
                    tool_input=tool_input,
                    content=f"Calling tool: {tool_name}{': ' + detail if detail else ''}",
                    handle=event_handle,
                    extra_data={"runtime_event_type": event_type},
                )
            ]

        if any(marker in event_type for marker in ("error", "failed", "failure")):
            content = self._extract_text(event) or f"{self._display_name} reported an error"
            return [
                AgentMessage(
                    type="result" if event_type.endswith(("failed", "failure")) else "assistant",
                    content=content,
                    data={"subtype": "error", "error_type": "GooseRuntimeError"},
                    resume_handle=event_handle,
                )
            ]

        if any(marker in event_type for marker in ("complete", "completed", "final", "done")):
            content = self._extract_text(event)
            if not content:
                return []
            return [
                AgentMessage(
                    type="result",
                    content=content,
                    data={"subtype": "success"},
                    resume_handle=event_handle,
                )
            ]

        if any(marker in event_type for marker in ("message", "assistant", "response", "output")):
            content = self._extract_text(event)
            if not content:
                return []
            return [AgentMessage(type="assistant", content=content, resume_handle=event_handle)]

        # Some Goose stream events may omit a stable event type.  Surface text
        # payloads conservatively and ignore metadata-only events.
        content = self._extract_text(event)
        if content:
            return [AgentMessage(type="assistant", content=content, resume_handle=event_handle)]
        return []


__all__ = ["GooseCliRuntime"]
