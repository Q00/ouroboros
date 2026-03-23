"""Cursor Agent CLI adapter for single-turn LLM completions.

Uses ``cursor-agent -p`` to make LLM calls through the user's Cursor
plan — no separate API key required. The model is whatever the user
has configured in their Cursor subscription.

This adapter is used for internal Ouroboros LLM calls (QA, ambiguity
scoring, interview question generation) when the cursor backend is
selected.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any

import structlog

from ouroboros.core.errors import ProviderError
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

_MAX_RETRIES = 3
_INITIAL_BACKOFF = 2.0


def _get_cursor_chat_model() -> str | None:
    """Read the most recently used IDE model from Cursor's internal state.

    Parses the ``cursorDiskKV`` table in Cursor's VS Code state database
    to find the latest ``providerOptions.cursor.modelName`` value.
    This reflects the model selected in the Cursor IDE chat window.

    Falls back to ``~/.cursor/cli-config.json`` (CLI default model),
    then ``None`` (cursor-agent auto) on any failure.
    """
    import json
    import re
    import sqlite3

    # Primary: IDE internal state (most recently used chat model)
    state_db = (
        Path.home()
        / "Library"
        / "Application Support"
        / "Cursor"
        / "User"
        / "globalStorage"
        / "state.vscdb"
    )
    try:
        conn = sqlite3.connect(str(state_db))
        conn.text_factory = bytes
        cur = conn.cursor()
        cur.execute(
            "SELECT key, value FROM cursorDiskKV ORDER BY rowid DESC LIMIT 2000"
        )
        pattern = re.compile(
            r'providerOptions":\{"cursor":\{"modelName":"([^"]+)"'
        )
        for _key, value in cur.fetchall():
            text = value.decode("utf-8", "ignore") if isinstance(value, bytes) else str(value)
            m = pattern.search(text)
            if m:
                conn.close()
                return m.group(1)
        conn.close()
    except Exception:
        pass

    # Fallback: CLI config
    cli_config = Path.home() / ".cursor" / "cli-config.json"
    try:
        data = json.loads(cli_config.read_text(encoding="utf-8"))
        model_id = data.get("model", {}).get("modelId")
        if model_id:
            return model_id
    except Exception:
        pass

    return None


class CursorAgentLLMAdapter:
    """LLM adapter using cursor-agent CLI for single-turn completions.

    Shells out to ``cursor-agent -p`` in non-interactive mode. Uses the
    user's Cursor plan and model — no API key required.
    """

    def __init__(
        self,
        cli_path: str | None = None,
        cwd: str | None = None,
        model: str | None = None,
        max_retries: int = _MAX_RETRIES,
        **kwargs: Any,
    ) -> None:
        check_cursor_agent_auth()
        self._cli_path = cli_path or self._resolve_cli_path()
        self._cwd = cwd or os.getcwd()
        self._model = model
        self._max_retries = max_retries

    @staticmethod
    def _resolve_cli_path() -> str:
        """Find cursor-agent binary."""
        import shutil

        path = shutil.which("cursor-agent")
        if path:
            return path
        home = Path.home()
        for candidate in (
            home / ".local" / "bin" / "cursor-agent",
            Path("/usr/local/bin/cursor-agent"),
        ):
            if candidate.exists():
                return str(candidate)
        return "cursor-agent"

    async def complete(
        self,
        messages: list[Message],
        config: CompletionConfig,
    ) -> Result[CompletionResponse, ProviderError]:
        """Run a single-turn completion via cursor-agent -p."""
        # Build prompt from messages
        prompt = self._build_prompt(messages, config)

        for attempt in range(1, self._max_retries + 1):
            try:
                result = await self._execute(prompt, config.model)
                return Result.ok(result)
            except ProviderError as e:
                if attempt < self._max_retries:
                    await asyncio.sleep(_INITIAL_BACKOFF * attempt)
                    continue
                return Result.err(e)

        return Result.err(ProviderError("Max retries exceeded", provider="cursor_agent"))

    async def _execute(
        self,
        prompt: str,
        model: str | None = None,
    ) -> CompletionResponse:
        """Spawn cursor-agent and collect output."""
        command = [self._cli_path, "-p", "-f"]

        cursor_model = _get_cursor_chat_model()
        if cursor_model:
            command.extend(["--model", cursor_model])

        command.extend(["--workspace", self._cwd])
        command.append(prompt)

        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=self._cwd,
            )
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                process.communicate(),
                timeout=120,
            )
        except FileNotFoundError:
            raise ProviderError(
                "cursor-agent not found. Install: curl https://cursor.com/install -fsSL | bash",
                provider="cursor_agent",
            )
        except asyncio.TimeoutError:
            raise ProviderError(
                "cursor-agent timed out after 120s",
                provider="cursor_agent",
            )

        stdout = stdout_bytes.decode("utf-8", errors="replace").strip()
        stderr = stderr_bytes.decode("utf-8", errors="replace").strip()

        if process.returncode != 0:
            raise ProviderError(
                stderr or f"cursor-agent exited with code {process.returncode}",
                provider="cursor_agent",
            )

        if not stdout:
            raise ProviderError(
                "cursor-agent returned empty response",
                provider="cursor_agent",
            )

        return CompletionResponse(
            content=stdout,
            model=model or "cursor-default",
            usage=UsageInfo(prompt_tokens=0, completion_tokens=0, total_tokens=0),
            raw_response={"stdout": stdout, "stderr": stderr},
        )

    @staticmethod
    def _build_prompt(messages: list[Message], config: CompletionConfig) -> str:
        """Flatten messages into a single prompt string."""
        parts: list[str] = []

        for msg in messages:
            if msg.role == MessageRole.SYSTEM:
                parts.append(msg.content)
            elif msg.role == MessageRole.USER:
                parts.append(msg.content)
            elif msg.role == MessageRole.ASSISTANT:
                parts.append(f"[Previous response]: {msg.content}")

        return "\n\n".join(parts)


def check_cursor_agent_auth() -> None:
    """Verify cursor-agent is authenticated. Raise ValueError if not.

    Used by both CursorAgentLLMAdapter and CursorAgentRuntime to ensure
    authentication before any cursor-agent CLI calls.
    """
    import shutil
    import subprocess

    cli = shutil.which("cursor-agent")
    if not cli:
        for candidate in (
            Path.home() / ".local" / "bin" / "cursor-agent",
            Path("/usr/local/bin/cursor-agent"),
        ):
            if candidate.exists():
                cli = str(candidate)
                break

    if not cli:
        raise ValueError(
            "cursor-agent CLI not found.\n"
            "Install it: curl https://cursor.com/install -fsSL | bash"
        )

    try:
        result = subprocess.run(
            [cli, "whoami"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode != 0 or "not logged in" in result.stdout.lower():
            raise ValueError(
                "cursor-agent is not authenticated.\n"
                "Run `cursor-agent login` in your terminal, "
                "or set CURSOR_API_KEY environment variable."
            )
    except subprocess.TimeoutExpired:
        pass  # Assume OK if check times out
