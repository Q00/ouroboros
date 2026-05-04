"""Kiro CLI LLM adapter via subprocess.

Calls ``kiro-cli chat --no-interactive`` for single-response completions.
Follows the same contract as CodexCliLLMAdapter / ClaudeCodeAdapter.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import re
import shutil

import structlog

from ouroboros.core.errors import ProviderError
from ouroboros.core.json_utils import extract_json_payload
from ouroboros.core.types import Result
from ouroboros.providers.base import (
    CompletionConfig,
    CompletionResponse,
    LLMAdapter,
    Message,
    MessageRole,
    UsageInfo,
)

log = structlog.get_logger(__name__)

# Kiro CLI in ``--no-interactive`` mode still emits terminal prompt markers
# and color escapes on stdout (e.g. ``\x1b[38;5;141m> \x1b[0m`` before the
# actual content). Downstream parsers — especially Ouroboros' Seed extractor,
# which matches on field prefixes like ``GOAL:`` — cannot see through the
# escape sequences and silently fail. Stripping SGR/CSI escapes here keeps
# response content clean without losing the underlying text.
_ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def _strip_ansi(text: str) -> str:
    """Remove ANSI CSI/SGR escape sequences from a string."""
    return _ANSI_ESCAPE_RE.sub("", text)


_DEFAULT_TIMEOUT = 120.0
_DEFAULT_MAX_RETRIES = 3
_MAX_JSON_RETRIES = 3
_RETRYABLE_EXIT_CODES = (1, 137)
_PROCESS_SHUTDOWN_TIMEOUT = 5.0

_MODEL_NAME_MAP: dict[str, str] = {
    "claude-sonnet-4-6": "claude-sonnet-4.6",
    "claude-opus-4-6": "claude-opus-4.6",
    "claude-sonnet-4-5": "claude-sonnet-4.5",
    "claude-opus-4-5": "claude-opus-4.5",
    "claude-haiku-4-5": "claude-haiku-4.5",
}


def _map_model_name(model: str) -> str:
    return _MODEL_NAME_MAP.get(model, model)


async def _kill_process(proc: asyncio.subprocess.Process) -> None:
    if proc.returncode is not None:
        return
    try:
        proc.kill()
        await asyncio.wait_for(proc.wait(), timeout=_PROCESS_SHUTDOWN_TIMEOUT)
    except (TimeoutError, ProcessLookupError):
        pass


class KiroCodeAdapter:
    """LLM adapter using Kiro CLI subprocess (no-interactive mode).

    Implements the LLMAdapter protocol for single-response completions.
    """

    def __init__(
        self,
        *,
        cli_path: str | Path | None = None,
        cwd: str | Path | None = None,
        timeout: float | None = None,
        max_retries: int = _DEFAULT_MAX_RETRIES,
    ) -> None:
        self._cli_path = self._resolve_cli_path(cli_path)
        self._cwd = str(Path(cwd).expanduser()) if cwd is not None else None
        self._timeout = timeout if timeout and timeout > 0 else _DEFAULT_TIMEOUT
        self._max_retries = max_retries
        log.info("kiro_adapter.init", cli_path=self._cli_path, cwd=self._cwd)

    def _resolve_cli_path(self, cli_path: str | Path | None) -> str:
        if cli_path:
            return str(cli_path)
        from ouroboros.config import get_kiro_cli_path

        configured = get_kiro_cli_path()
        if configured:
            return configured
        return shutil.which("kiro-cli") or "kiro-cli"

    async def complete(
        self,
        messages: list[Message],
        config: CompletionConfig,
    ) -> Result[CompletionResponse, ProviderError]:
        """Make a completion request via Kiro CLI subprocess."""
        prompt = self._build_prompt(messages, config)
        cmd = self._build_cmd(prompt, config)
        env = {**os.environ, "OUROBOROS_SUBAGENT": "1"}
        cwd = self._cwd or os.getcwd()
        requires_json = bool(
            config.response_format
            and config.response_format.get("type") in ("json_schema", "json_object")
        )

        last_error: ProviderError | None = None
        max_attempts = self._max_retries + (_MAX_JSON_RETRIES if requires_json else 0)

        for attempt in range(max_attempts):
            proc: asyncio.subprocess.Process | None = None
            try:
                proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=cwd,
                    env=env,
                )
                stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=self._timeout)
            except TimeoutError:
                if proc:
                    await _kill_process(proc)
                last_error = ProviderError("Kiro CLI timed out", details={"attempt": attempt + 1})
                log.warning("kiro_adapter.timeout", attempt=attempt + 1)
                continue
            except FileNotFoundError:
                return Result.err(
                    ProviderError(
                        f"Kiro CLI not found at: {self._cli_path}",
                        details={"cli_path": self._cli_path},
                    )
                )

            if proc.returncode != 0:
                err_msg = stderr.decode(errors="replace").strip()
                if proc.returncode in _RETRYABLE_EXIT_CODES and attempt < self._max_retries - 1:
                    last_error = ProviderError(
                        f"Kiro CLI exited with code {proc.returncode}: {err_msg}",
                    )
                    log.warning(
                        "kiro_adapter.retrying",
                        code=proc.returncode,
                        attempt=attempt + 1,
                    )
                    await asyncio.sleep(2**attempt)
                    continue
                return Result.err(
                    ProviderError(
                        f"Kiro CLI failed (exit {proc.returncode}): {err_msg}",
                        details={"stderr": err_msg, "exit_code": proc.returncode},
                    )
                )

            content = _strip_ansi(stdout.decode(errors="replace")).strip()
            # The Kiro prompt marker "> " sometimes survives the escape strip
            # when the CSI reset is placed before, not after, the marker.
            if content.startswith("> "):
                content = content[2:].lstrip()
            if not content:
                last_error = ProviderError("Empty response from Kiro CLI")
                log.warning("kiro_adapter.empty", attempt=attempt + 1)
                continue

            # JSON enforcement: extract valid JSON when response_format requires it
            if requires_json:
                extracted = extract_json_payload(content)
                if extracted is None:
                    last_error = ProviderError(
                        "Response does not contain valid JSON",
                        details={"content_preview": content[:200]},
                    )
                    log.warning("kiro_adapter.json_extraction_failed", attempt=attempt + 1)
                    continue
                content = extracted

            return Result.ok(
                CompletionResponse(
                    content=content,
                    model=config.model,
                    usage=UsageInfo(prompt_tokens=0, completion_tokens=0, total_tokens=0),
                    finish_reason="stop",
                )
            )

        return Result.err(last_error or ProviderError("Max retries exceeded"))

    def _build_cmd(self, prompt: str, config: CompletionConfig) -> list[str]:
        cmd = [self._cli_path, "chat", "--no-interactive"]
        if config.model and config.model != "default":
            cmd.extend(["--model", _map_model_name(config.model)])
        cmd.append(prompt)
        return cmd

    def _build_prompt(self, messages: list[Message], config: CompletionConfig | None = None) -> str:
        parts: list[str] = []
        for msg in messages:
            if msg.role == MessageRole.SYSTEM:
                parts.append(f"<system>\n{msg.content}\n</system>")
            elif msg.role == MessageRole.USER:
                parts.append(f"User: {msg.content}")
            elif msg.role == MessageRole.ASSISTANT:
                parts.append(f"Assistant: {msg.content}")

        # Inject JSON schema instruction when response_format requires it
        if config and config.response_format:
            fmt_type = config.response_format.get("type")
            if fmt_type == "json_schema":
                schema = config.response_format.get("json_schema", {})
                top_type = schema.get("type", "object")
                type_noun = {"array": "JSON array", "object": "JSON object"}.get(
                    top_type, "JSON value"
                )
                parts.append(
                    f"Respond with ONLY a valid {type_noun} matching this schema. "
                    "No markdown fences, headers, or explanatory text.\n\n"
                    f"JSON schema:\n{json.dumps(schema, indent=2, sort_keys=True)}"
                )
            elif fmt_type == "json_object":
                parts.append(
                    "Respond with ONLY a valid JSON object. "
                    "No markdown fences, headers, or explanatory text."
                )

        return "\n\n".join(parts)


# Ensure protocol compliance
_: type[LLMAdapter] = KiroCodeAdapter  # type: ignore[assignment]

__all__ = ["KiroCodeAdapter"]
