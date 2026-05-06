"""Hermes CLI adapter for LLM completion using local Hermes authentication."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
import os
from pathlib import Path
import re
import shutil

import structlog

from ouroboros.config import get_hermes_cli_path
from ouroboros.core.errors import ProviderError
from ouroboros.core.security import MAX_LLM_RESPONSE_LENGTH, InputValidator
from ouroboros.core.types import Result
from ouroboros.orchestrator.hermes_runtime import _parse_quiet_output
from ouroboros.providers.base import (
    CompletionConfig,
    CompletionResponse,
    Message,
    MessageRole,
    UsageInfo,
)
from ouroboros.providers.codex_cli_stream import terminate_process
from ouroboros.providers.profiles import resolve_completion_profile_result

log = structlog.get_logger()

_DEFAULT_MODEL = "default"
_MAX_OUROBOROS_DEPTH = 5
_SAFE_MODEL_NAME_PATTERN = re.compile(r"^[A-Za-z0-9_./:@-]+$")


class HermesCliLLMAdapter:
    """LLM completion adapter backed by ``hermes chat -Q``."""

    _provider_name = "hermes_cli"
    _display_name = "Hermes CLI"
    _default_cli_name = "hermes"
    _process_shutdown_timeout_seconds = 5.0

    def __init__(
        self,
        *,
        cli_path: str | Path | None = None,
        model: str | None = None,
        cwd: str | Path | None = None,
        max_turns: int = 1,
        timeout: float | None = 120.0,
        max_retries: int = 3,
        on_message: Callable[[str, str], None] | None = None,
        allowed_tools: list[str] | None = None,
    ) -> None:
        self._cli_path = self._resolve_cli_path(cli_path)
        self._model = model or _DEFAULT_MODEL
        self._cwd = Path(cwd).resolve() if cwd else None
        self._max_turns = max_turns
        self._timeout = timeout
        self._max_retries = max_retries
        self._on_message = on_message
        self._allowed_tools = tuple(allowed_tools) if allowed_tools is not None else None

        log.info(
            "hermes_cli_adapter.initialized",
            cli_path=str(self._cli_path),
            model=self._model,
        )
        if self._allowed_tools is not None:
            log.warning(
                "hermes_cli_adapter.tool_envelope_unsupported",
                allowed_tools=list(self._allowed_tools),
                reason=(
                    "Hermes CLI quiet output does not expose tool-use events "
                    "for post-hoc envelope auditing. Pass allowed_tools=None "
                    "for Hermes sessions."
                ),
            )

    async def complete(
        self,
        messages: list[Message],
        config: CompletionConfig,
    ) -> Result[CompletionResponse, ProviderError]:
        """Make a single-turn completion request through Hermes CLI."""
        if self._allowed_tools is not None:
            return Result.err(
                ProviderError(
                    message=(
                        "Hermes CLI adapter does not support allowed_tools envelopes; "
                        "quiet mode cannot audit tool-use violations"
                    ),
                    provider=self._provider_name,
                    details={"allowed_tools": list(self._allowed_tools)},
                )
            )

        profile_result = resolve_completion_profile_result(config, backend="hermes")
        if profile_result.is_err:
            return Result.err(profile_result.error)
        config = profile_result.value.config

        prompt = self._build_prompt(messages)
        model = self._resolve_model(config.model)
        last_error: ProviderError | None = None

        for attempt in range(max(1, self._max_retries)):
            max_turns = config.max_turns or self._max_turns
            result = await self._execute_request(prompt, model, max_turns=max_turns)
            if result.is_ok:
                return result

            last_error = result.error
            if attempt < self._max_retries - 1:
                await asyncio.sleep(2**attempt)

        return Result.err(last_error or ProviderError(message="Max retries exceeded"))

    async def _execute_request(
        self,
        prompt: str,
        model: str,
        *,
        max_turns: int,
    ) -> Result[CompletionResponse, ProviderError]:
        cmd = self._build_command(prompt, model, max_turns=max_turns)
        log.debug("hermes_cli_adapter.spawning", cmd=cmd)

        try:
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(self._cwd) if self._cwd else None,
                env=self._build_env(),
            )
        except FileNotFoundError:
            return Result.err(
                ProviderError(
                    message=f"Hermes CLI not found at '{self._cli_path}'",
                    provider=self._provider_name,
                )
            )
        except Exception as exc:
            return Result.err(
                ProviderError(
                    message=f"Failed to spawn Hermes CLI: {exc}",
                    provider=self._provider_name,
                )
            )

        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=self._timeout)
        except TimeoutError:
            await terminate_process(
                process,
                shutdown_timeout=self._process_shutdown_timeout_seconds,
            )
            return Result.err(
                ProviderError(
                    message=f"Hermes CLI timed out after {self._timeout}s",
                    provider=self._provider_name,
                    details={"timeout_seconds": self._timeout},
                )
            )

        stdout_text = self._decode(stdout)
        stderr_text = self._decode(stderr)
        returncode = getattr(process, "returncode", None)
        if returncode not in (0, None):
            return Result.err(
                ProviderError(
                    message=(
                        f"Hermes CLI exited with code {returncode}"
                        + (f": {stderr_text.strip()}" if stderr_text.strip() else "")
                    ),
                    provider=self._provider_name,
                    details={"returncode": returncode, "stderr": stderr_text},
                )
            )

        content, session_id = _parse_quiet_output(stdout_text)
        if not content:
            return Result.err(
                ProviderError(
                    message="Hermes CLI produced an empty response",
                    provider=self._provider_name,
                    details={"session_id": session_id, "stderr": stderr_text},
                )
            )

        is_valid, _ = InputValidator.validate_llm_response(content)
        if not is_valid:
            log.warning(
                "llm.response.truncated",
                model=model,
                original_length=len(content),
                max_length=MAX_LLM_RESPONSE_LENGTH,
            )
            content = content[:MAX_LLM_RESPONSE_LENGTH]

        if self._on_message and content.strip():
            self._on_message("thinking", content.strip())

        return Result.ok(
            CompletionResponse(
                content=content,
                model=model,
                usage=UsageInfo(prompt_tokens=0, completion_tokens=0, total_tokens=0),
                finish_reason="stop",
                raw_response={"session_id": session_id},
            )
        )

    def _build_command(self, prompt: str, model: str, *, max_turns: int) -> list[str]:
        cmd = [
            str(self._cli_path),
            "chat",
            "-Q",
            "--source",
            "tool",
            "--max-turns",
            str(max(1, max_turns)),
            "-q",
            prompt,
        ]
        if model and model != "default":
            cmd.extend(["--model", model])
        return cmd

    def _build_prompt(self, messages: list[Message]) -> str:
        parts: list[str] = []
        system_msgs = [m for m in messages if m.role == MessageRole.SYSTEM]
        non_system = [m for m in messages if m.role != MessageRole.SYSTEM]

        system_parts: list[str] = []
        envelope_block = self._render_tool_envelope_block()
        if envelope_block:
            system_parts.append(envelope_block)
        system_parts.extend(m.content for m in system_msgs)
        if system_parts:
            parts.append("<system>\n" + "\n\n".join(system_parts) + "\n</system>")

        for msg in non_system:
            if msg.role == MessageRole.USER:
                parts.append(f"User: {msg.content}")
            elif msg.role == MessageRole.ASSISTANT:
                parts.append(f"Assistant: {msg.content}")
        return "\n\n".join(parts)

    def _render_tool_envelope_block(self) -> str | None:
        if self._allowed_tools is None:
            return None
        if not self._allowed_tools:
            return (
                "<tool_envelope>\n"
                "You are not permitted to invoke any tools in this session. "
                "Respond using only text.\n"
                "</tool_envelope>"
            )
        allowed_list = ", ".join(self._allowed_tools)
        return (
            "<tool_envelope>\n"
            f"You may ONLY invoke the following tools in this session: {allowed_list}. "
            "Do not invoke any other tool under any circumstances.\n"
            "</tool_envelope>"
        )

    def _build_env(self) -> dict[str, str]:
        env = os.environ.copy()
        for key in ("OUROBOROS_AGENT_RUNTIME", "OUROBOROS_LLM_BACKEND"):
            env.pop(key, None)
        try:
            depth = int(env.get("_OUROBOROS_DEPTH", "0")) + 1
        except (ValueError, TypeError):
            depth = 1
        if depth > _MAX_OUROBOROS_DEPTH:
            raise ProviderError(
                message=f"Maximum Ouroboros nesting depth ({_MAX_OUROBOROS_DEPTH}) exceeded",
                provider=self._provider_name,
                details={"depth": depth},
            )
        env["_OUROBOROS_DEPTH"] = str(depth)
        return env

    def _resolve_model(self, config_model: str) -> str:
        if not config_model or config_model == "default":
            return self._model
        if not _SAFE_MODEL_NAME_PATTERN.match(config_model):
            log.warning(
                "hermes_cli_adapter.unsafe_model_name",
                model=config_model,
                fallback=self._model,
            )
            return self._model
        return config_model

    def _resolve_cli_path(self, cli_path: str | Path | None) -> Path:
        if cli_path:
            candidate = Path(cli_path).expanduser()
            if candidate.parent == Path("."):
                return candidate
            return candidate.resolve()
        configured = get_hermes_cli_path()
        if configured:
            candidate = Path(configured).expanduser()
            if candidate.parent == Path("."):
                return candidate
            return candidate.resolve()
        found = shutil.which(self._default_cli_name)
        if found:
            return Path(found)
        return Path(self._default_cli_name)

    @staticmethod
    def _decode(value: bytes | str | None) -> str:
        if value is None:
            return ""
        if isinstance(value, str):
            return value
        return value.decode("utf-8", errors="replace")


__all__ = ["HermesCliLLMAdapter"]
