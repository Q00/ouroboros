"""Kiro CLI LLM adapter via subprocess.

Calls ``kiro-cli chat --no-interactive`` for single-response completions.
Follows the same contract as CodexCliLLMAdapter / ClaudeCodeAdapter.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
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
_TOOL_USE_MARKER_RE = re.compile(r'"type"\s*:\s*"tool_use"|tool_use')
_TOOL_NAME_RE = re.compile(r'"(?:name|tool)"\s*:\s*"([^"]+)"')
_STRIPPED_ENV_KEYS = (
    "OUROBOROS_AGENT_RUNTIME",
    "OUROBOROS_LLM_BACKEND",
    "OUROBOROS_RUNTIME",
    "CLAUDECODE",
)
_MAX_OUROBOROS_DEPTH = 5

_KIRO_TOOL_NAME_MAP: dict[str, str] = {
    "bash": "shell",
    "edit": "write",
    "glob": "read",
    "grep": "grep",
    "ls": "read",
    "multiedit": "write",
    "read": "read",
    "shell": "shell",
    "write": "write",
}


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
        allowed_tools: list[str] | None = None,
        permission_mode: str | None = None,
        max_turns: int = 1,
        on_message: Callable[[str, str], None] | None = None,
        timeout: float | None = None,
        max_retries: int = _DEFAULT_MAX_RETRIES,
    ) -> None:
        self._cli_path = self._resolve_cli_path(cli_path)
        self._cwd = str(Path(cwd).expanduser()) if cwd is not None else None
        self._allowed_tools = list(allowed_tools) if allowed_tools is not None else None
        self._permission_mode = permission_mode
        self._max_turns = max_turns
        self._on_message = on_message
        self._timeout = timeout if timeout and timeout > 0 else _DEFAULT_TIMEOUT
        self._max_retries = max_retries
        log.info("kiro_adapter.init", cli_path=self._cli_path, cwd=self._cwd)
        if self._allowed_tools is not None:
            log.warning(
                "kiro_adapter.soft_tool_enforcement",
                allowed_tools=list(self._allowed_tools),
                reason=(
                    "Kiro CLI has no native allowed_tools flag; the envelope "
                    "is injected as a prompt directive and detectable tool-use "
                    "markers are audited post-hoc. Enforcement is cooperative, "
                    "not mandatory."
                ),
            )

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
        env = self._build_child_env()
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

            self._audit_tool_envelope_violations(content)
            if self._on_message:
                self._on_message("assistant", content)

            return Result.ok(
                CompletionResponse(
                    content=content,
                    model=config.model,
                    usage=UsageInfo(prompt_tokens=0, completion_tokens=0, total_tokens=0),
                    finish_reason="stop",
                )
            )

        return Result.err(last_error or ProviderError("Max retries exceeded"))

    def _build_child_env(self) -> dict[str, str]:
        """Build an isolated environment for child Kiro LLM processes."""
        env = os.environ.copy()
        for key in _STRIPPED_ENV_KEYS:
            env.pop(key, None)
        try:
            depth = int(env.get("_OUROBOROS_DEPTH", "0")) + 1
        except (ValueError, TypeError):
            depth = 1
        if depth > _MAX_OUROBOROS_DEPTH:
            msg = f"Maximum Ouroboros nesting depth ({_MAX_OUROBOROS_DEPTH}) exceeded"
            raise RuntimeError(msg)
        env["_OUROBOROS_DEPTH"] = str(depth)
        env["OUROBOROS_SUBAGENT"] = "1"
        return env

    def _build_cmd(self, prompt: str, config: CompletionConfig) -> list[str]:
        cmd = [self._cli_path, "chat", "--no-interactive"]
        if self._allowed_tools is not None:
            # Kiro's headless mode exposes native trust flags.  Passing the
            # flag even for an empty list makes the caller's explicit
            # no-tools envelope visible at the CLI boundary instead of relying
            # only on prompt wording.
            cmd.append(f"--trust-tools={self._kiro_trust_tools_arg()}")
        elif self._permission_mode == "default":
            cmd.append("--trust-tools=")
        elif self._permission_mode in {"acceptEdits", "bypassPermissions"}:
            cmd.append("--trust-all-tools")
        if config.model and config.model != "default":
            cmd.extend(["--model", _map_model_name(config.model)])
        cmd.append(prompt)
        return cmd

    def _kiro_trust_tools_arg(self) -> str:
        if not self._allowed_tools:
            return ""

        mapped_tools: list[str] = []
        seen: set[str] = set()
        for tool in self._allowed_tools:
            normalized = tool.strip().lower().replace("_", "-")
            mapped = _KIRO_TOOL_NAME_MAP.get(normalized.replace("-", ""), normalized)
            if mapped not in seen:
                mapped_tools.append(mapped)
                seen.add(mapped)
        return ",".join(mapped_tools)

    def _build_prompt(self, messages: list[Message], config: CompletionConfig | None = None) -> str:
        parts: list[str] = []
        for msg in messages:
            if msg.role == MessageRole.SYSTEM:
                parts.append(f"<system>\n{msg.content}\n</system>")
            elif msg.role == MessageRole.USER:
                parts.append(f"User: {msg.content}")
            elif msg.role == MessageRole.ASSISTANT:
                parts.append(f"Assistant: {msg.content}")

        if self._allowed_tools is not None:
            if self._allowed_tools:
                parts.append(
                    "Tool constraints: Limit tool usage to ONLY the following tools:\n"
                    + "\n".join(f"- {tool}" for tool in self._allowed_tools)
                )
            else:
                parts.append("Tool constraints: Do NOT use any tools. Respond with text only.")

        if self._max_turns == 1:
            parts.append(
                "Execution constraints: Respond in a single turn. Do not ask follow-up questions."
            )
        elif self._max_turns > 1:
            parts.append(
                f"Execution constraints: Complete your response within {self._max_turns} turns maximum."
            )

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

    def _audit_tool_envelope_violations(self, content: str) -> None:
        """Warn when Kiro output exposes tool-use markers outside the envelope.

        Kiro's non-interactive stdout is not a structured event stream, so this
        is intentionally best-effort. The prompt directive is the primary soft
        envelope; this hook catches JSON-ish ``tool_use`` markers if Kiro
        surfaces them in output.
        """
        if self._allowed_tools is None or not _TOOL_USE_MARKER_RE.search(content):
            return

        allowed = frozenset(self._allowed_tools)
        tool_names = _TOOL_NAME_RE.findall(content)
        if not tool_names and not allowed:
            log.warning(
                "kiro_adapter.tool_envelope_violation",
                tool=None,
                allowed_tools=[],
                reason="Kiro output included a tool_use marker despite an empty allowed_tools envelope.",
            )
            return

        for tool_name in tool_names:
            if tool_name not in allowed:
                log.warning(
                    "kiro_adapter.tool_envelope_violation",
                    tool=tool_name,
                    allowed_tools=list(self._allowed_tools),
                )


# Ensure protocol compliance
_: type[LLMAdapter] = KiroCodeAdapter  # type: ignore[assignment]

__all__ = ["KiroCodeAdapter"]
