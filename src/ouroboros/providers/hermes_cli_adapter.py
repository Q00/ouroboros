"""Hermes CLI adapter for LLM completion using local Hermes authentication."""

from __future__ import annotations

from pathlib import Path

from ouroboros.core.errors import ProviderError
from ouroboros.core.types import Result
from ouroboros.providers.base import (
    CompletionConfig,
    CompletionResponse,
    Message,
    UsageInfo,
)


class HermesCliLLMAdapter:
    """LLM adapter backed by a single-turn Hermes CLI task."""

    _provider_name = "hermes_cli"
    _display_name = "Hermes CLI"

    def __init__(
        self,
        *,
        cli_path: str | Path | None = None,
        cwd: str | Path | None = None,
        model: str | None = None,
        timeout: float | None = None,
    ) -> None:
        from ouroboros.orchestrator.hermes_runtime import HermesCliRuntime

        self._runtime = HermesCliRuntime(
            cli_path=cli_path,
            cwd=cwd,
            model=model,
            startup_output_timeout_seconds=timeout,
            stdout_idle_timeout_seconds=timeout,
        )
        self._cli_path = self._runtime._cli_path
        self._cwd = self._runtime.working_directory

    async def complete(
        self,
        messages: list[Message],
        config: CompletionConfig,
    ) -> Result[CompletionResponse, ProviderError]:
        """Run Hermes once and adapt the final task result to completion output."""
        prompt = "\n\n".join(message.content for message in messages if message.content.strip())
        model = None if config.model == "default" else config.model
        if model != self._runtime._model:
            self._runtime._model = model

        try:
            result = await self._runtime.execute_task_to_result(prompt, tools=[])
        except FileNotFoundError as exc:
            return Result.err(
                ProviderError(
                    message=(
                        f"{self._display_name} not found at {self._cli_path}. "
                        "Install Hermes or configure OUROBOROS_HERMES_CLI_PATH."
                    ),
                    provider=self._provider_name,
                    details={"error": str(exc), "cli_path": self._cli_path},
                )
            )
        except Exception as exc:
            return Result.err(
                ProviderError(
                    message=f"{self._display_name} failed: {exc}",
                    provider=self._provider_name,
                    details={"error_type": type(exc).__name__},
                )
            )

        if not result.is_ok:
            return Result.err(result.error)

        content = result.value.final_message
        return Result.ok(
            CompletionResponse(
                content=content,
                model=model or "hermes-default",
                usage=UsageInfo(prompt_tokens=0, completion_tokens=0, total_tokens=0),
                raw_response={"session_id": result.value.session_id},
            )
        )
