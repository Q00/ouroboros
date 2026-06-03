"""Pi CLI adapter for LLM completion via pi.dev JSON mode."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ouroboros.config import get_pi_cli_path
from ouroboros.core.errors import ProviderError
from ouroboros.core.types import Result
from ouroboros.providers.base import CompletionConfig, CompletionResponse, Message
from ouroboros.providers.codex_cli_adapter import CodexCliLLMAdapter


class PiLLMAdapter(CodexCliLLMAdapter):
    """LLM adapter backed by ``pi --mode json``.

    Pi uses the same JSONL event stream family as the runtime adapter but is
    exposed here as an LLM-only provider so interview/planning/evaluation roles
    can select ``--llm-backend pi``.
    """

    _provider_name = "pi"
    _display_name = "Pi CLI"
    _default_cli_name = "pi"
    _tempfile_prefix = "ouroboros-pi-llm-"
    _schema_tempfile_prefix = "ouroboros-pi-schema-"
    _log_namespace = "pi_llm_adapter"
    _completion_profile_backend = "pi"

    def __init__(
        self,
        *,
        cli_path: str | Path | None = None,
        cwd: str | Path | None = None,
        permission_mode: str | None = None,
        allowed_tools: list[str] | None = None,
        max_turns: int = 1,
        on_message: Any | None = None,
        max_retries: int = 3,
        ephemeral: bool = True,
        timeout: float | None = None,
        runtime_profile: str | None = None,
    ) -> None:
        del runtime_profile
        super().__init__(
            cli_path=cli_path,
            cwd=cwd,
            permission_mode=permission_mode,
            allowed_tools=allowed_tools,
            max_turns=max_turns,
            on_message=on_message,
            max_retries=max_retries,
            ephemeral=ephemeral,
            timeout=timeout,
            runtime_profile=None,
        )
        self._last_pi_event_kind: str | None = None

    def _get_configured_cli_path(self) -> str | None:
        """Resolve Pi CLI path from config helpers."""
        return get_pi_cli_path()

    def _resolve_permission_mode(self, permission_mode: str | None) -> str:
        """Pi currently has no separate permission-mode flag surface."""
        return (permission_mode or "default").strip() or "default"

    def _build_permission_args(self) -> list[str]:
        """Pi JSON mode does not currently expose Codex-style permission flags."""
        return []

    def _prompt_stdin_bytes(self, prompt: str) -> bytes | None:
        """Pi JSON mode receives the prompt as a positional argument."""
        del prompt
        return None

    def _build_command(
        self,
        *,
        output_last_message_path: str,
        output_schema_path: str | None,
        model: str | None,
        profile: str | None = None,
        prompt: str | None = None,
    ) -> list[str]:
        """Build ``pi --mode json <prompt>``.

        ``output_last_message_path`` and schema/profile parameters are accepted
        for factory compatibility with :class:`CodexCliLLMAdapter`; Pi emits its
        response on JSONL stdout instead.
        """
        del output_last_message_path, output_schema_path, profile
        command = [self._cli_path, "--mode", "json"]
        if model:
            command.extend(["--model", model])
        command.append(prompt or "")
        return command

    def _update_last_content(self, last_content: str, event_content: str) -> str:
        """Accumulate streaming deltas but replace them with terminal Pi content.

        Pi JSON mode can emit both whitespace-preserving ``message_update``
        deltas and a final ``agent_end`` / ``message_end`` full message. The
        inherited Codex loop only passes extracted text into this hook, so this
        adapter records the most recent Pi event kind in ``_extract_text`` and
        uses it here to avoid returning duplicated ``delta + final`` content.
        """
        if not event_content:
            return last_content
        if self._last_pi_event_kind == "final":
            return event_content
        return f"{last_content}{event_content}" if last_content else event_content

    async def complete(
        self,
        messages: list[Message],
        config: CompletionConfig,
    ) -> Result[CompletionResponse, ProviderError]:
        """Make a Pi completion request, failing closed on unsupported schemas."""
        self._last_pi_event_kind = None
        if config.response_format:
            response_format_type = config.response_format.get("type")
            return Result.err(
                ProviderError(
                    message=(
                        "Pi CLI LLM backend does not currently support structured "
                        "response_format requests"
                    ),
                    provider=self._provider_name,
                    details={"response_format_type": response_format_type},
                )
            )
        return await super().complete(messages, config)

    def _extract_session_id_from_event(self, event: dict[str, Any]) -> str | None:
        if event.get("type") == "session" and isinstance(event.get("id"), str):
            return event["id"]
        return None

    def _extract_text_from_message(self, message: dict[str, Any]) -> str:
        """Extract assistant text from a Pi transcript message."""
        content = message.get("content") or message.get("text") or ""
        if isinstance(content, str) and content.strip():
            return content.strip()
        if isinstance(content, list):
            texts: list[str] = []
            for item in content:
                if isinstance(item, str):
                    texts.append(item)
                elif isinstance(item, dict):
                    text = item.get("text") or item.get("content")
                    if isinstance(text, str) and item.get("type") in {None, "text"}:
                        texts.append(text)
            return "".join(texts).strip()
        return ""

    def _extract_content_delta(self, event: dict[str, Any]) -> str:
        """Extract streaming text with parity to ``PiRuntime`` JSON parsing."""
        if event.get("type") != "message_update":
            return ""

        assistant_event = event.get("assistantMessageEvent")
        if isinstance(assistant_event, dict):
            delta = assistant_event.get("delta")
            if isinstance(delta, str):
                return delta
            text = assistant_event.get("text") or assistant_event.get("content")
            if isinstance(text, str):
                return text

        delta = event.get("delta") or event.get("content") or event.get("text")
        if isinstance(delta, str):
            return delta
        if isinstance(delta, dict):
            text = delta.get("text") or delta.get("content")
            return text if isinstance(text, str) else ""
        return ""

    def _extract_final_text(self, event: dict[str, Any]) -> str:
        """Extract only terminal assistant content from Pi final events."""
        event_type = event.get("type")
        if event_type in {"message_end", "turn_end"}:
            message = event.get("message")
            if isinstance(message, dict) and message.get("role") == "assistant":
                return self._extract_text_from_message(message)
            return ""
        if event_type == "agent_end":
            messages = event.get("messages")
            if isinstance(messages, list):
                for message in reversed(messages):
                    if isinstance(message, dict) and message.get("role") == "assistant":
                        text = self._extract_text_from_message(message)
                        if text:
                            return text
            return ""
        return ""

    def _extract_text(self, value: object) -> str:
        """Extract content from documented Pi JSONL events."""
        if isinstance(value, dict):
            event_type = value.get("type")
            self._last_pi_event_kind = None
            if event_type == "message_update":
                self._last_pi_event_kind = "delta"
                return self._extract_content_delta(value)
            if event_type in {"message_end", "turn_end", "agent_end"}:
                self._last_pi_event_kind = "final"
                return self._extract_final_text(value)
            # Pi control/metadata events (for example `session`) must never fall
            # through to the broad Codex extractor, which treats shallow string
            # fields such as `type` and `id` as user-visible completion text.
            return ""
        self._last_pi_event_kind = None
        return super()._extract_text(value)


__all__ = ["PiLLMAdapter"]
