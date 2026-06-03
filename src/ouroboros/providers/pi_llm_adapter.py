"""Pi CLI adapter for LLM completion via pi.dev JSON mode."""

from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError as JsonSchemaValidationError

from ouroboros.config import get_pi_cli_path
from ouroboros.core.errors import ProviderError
from ouroboros.core.json_utils import extract_json_payload
from ouroboros.core.types import Result
from ouroboros.providers.base import CompletionConfig, CompletionResponse, Message, MessageRole
from ouroboros.providers.codex_cli_adapter import CodexCliLLMAdapter
from ouroboros.providers.profiles import resolve_completion_profile_result


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
        # Ouroboros normalizes generic cross-provider defaults to the local-CLI
        # sentinel "default". Pi should use its own backend default in that case
        # rather than forwarding Anthropic-oriented or sentinel model names.
        if model and model != "default":
            command.extend(["--model", model])
        command.append(prompt or "")
        return command

    def _build_response_format_directive(
        self,
        response_format: dict[str, object] | None,
    ) -> str | None:
        """Translate response_format into Pi prompt instructions.

        Pi JSON mode does not expose a Codex-style hard ``--output-schema``
        flag, so structured output is cooperatively enforced through the
        prompt and validated after extraction.
        """
        if not response_format:
            return None
        fmt_type = response_format.get("type")
        if fmt_type == "json_object":
            return (
                "Respond with ONLY a valid JSON object. Do not use markdown fences, "
                "headers, or explanatory text."
            )
        if fmt_type == "json_schema":
            schema = response_format.get("json_schema")
            if not isinstance(schema, dict):
                return None
            schema_payload = schema.get("schema") if isinstance(schema.get("schema"), dict) else schema
            top_type = schema_payload.get("type", "object") if isinstance(schema_payload, dict) else "object"
            type_noun = {
                "array": "JSON array",
                "object": "JSON object",
            }.get(str(top_type), "JSON value")
            try:
                rendered = json.dumps(schema_payload, indent=2, sort_keys=True)
            except (TypeError, ValueError):
                rendered = str(schema_payload)
            return (
                f"Respond with ONLY a valid {type_noun} that matches this schema. "
                "Do not use markdown fences, headers, or explanatory text.\n\n"
                f"JSON schema:\n{rendered}"
            )
        return None

    def _validate_response_format_payload(
        self,
        payload: str,
        response_format: dict[str, object],
    ) -> str | None:
        """Validate extracted JSON against the requested response_format."""
        try:
            parsed = json.loads(payload)
        except json.JSONDecodeError as exc:
            return f"invalid JSON: {exc}"

        fmt_type = response_format.get("type")
        if fmt_type == "json_object":
            return None if isinstance(parsed, dict) else "expected a JSON object"

        if fmt_type == "json_schema":
            schema = response_format.get("json_schema")
            if not isinstance(schema, dict):
                return "json_schema response_format is missing a schema object"
            schema_payload = schema.get("schema") if isinstance(schema.get("schema"), dict) else schema
            try:
                Draft202012Validator(schema_payload).validate(parsed)
            except JsonSchemaValidationError as exc:
                return exc.message
        return None

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
        """Make a Pi completion request, including soft structured output support."""
        self._last_pi_event_kind = None
        if not config.response_format:
            return await super().complete(messages, config)

        profile_result = resolve_completion_profile_result(
            config,
            backend=self._completion_profile_backend,
        )
        if profile_result.is_err:
            return Result.err(profile_result.error)
        effective_config = profile_result.value.config

        directive = self._build_response_format_directive(effective_config.response_format)
        if not directive:
            return Result.err(
                ProviderError(
                    message="Unsupported Pi structured response_format request",
                    provider=self._provider_name,
                    details={
                        "response_format_type": effective_config.response_format.get("type"),
                    },
                )
            )

        patched_messages = [Message(role=MessageRole.SYSTEM, content=directive), *messages]
        patched_config = replace(effective_config, response_format=None)
        attempts = max(1, self._max_retries)
        last_response_preview = ""
        for _attempt in range(attempts):
            self._last_pi_event_kind = None
            result = await super().complete(patched_messages, patched_config)
            if result.is_err:
                return result
            last_response_preview = result.value.content[:240]
            extracted = extract_json_payload(result.value.content)
            if not extracted:
                continue
            validation_error = self._validate_response_format_payload(
                extracted,
                effective_config.response_format,
            )
            if validation_error is None:
                return Result.ok(replace(result.value, content=extracted))

        return Result.err(
            ProviderError(
                message="JSON format required but Pi returned non-conforming output",
                provider=self._provider_name,
                details={"last_response_preview": last_response_preview},
            )
        )

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
            assistant_event_type = assistant_event.get("type")
            if assistant_event_type and assistant_event_type != "text_delta":
                return ""
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
