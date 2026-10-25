"""Megacode harness adapter for LLM completion.

This adapter shells out to ``harness -p`` in headless mode so Ouroboros
LLM-only calls (interview, seed, QA, evaluation, consensus roles) run on the
same model and login as the user's harness sessions, including hosted
``mega:*`` models.

Each call runs in :data:`MEGACODE_LLM_MODE`, a ``[modes.<name>]`` table that
``ouroboros setup --runtime megacode`` writes with read-only tools and nothing
gated. The prompt goes in on stdin and the answer is the ``result`` record of
``--output-format stream-json``.
"""

from __future__ import annotations

from dataclasses import replace
import json
import os
from pathlib import Path
import shutil
from typing import Any

import structlog

from ouroboros.config import get_megacode_cli_path
from ouroboros.core.errors import ProviderError
from ouroboros.core.json_utils import extract_json_payload
from ouroboros.core.types import Result
from ouroboros.providers.base import CompletionConfig, CompletionResponse, Message, MessageRole
from ouroboros.providers.codex_cli_adapter import CodexCliLLMAdapter
from ouroboros.providers.profiles import resolve_completion_profile_result
from ouroboros.providers.response_format import validate_response_format_payload

log = structlog.get_logger()

#: ``[modes.<name>]`` table LLM completions run in (written by setup).
MEGACODE_LLM_MODE = "ouroboros-llm"

# stream-json record types that never carry the completion answer.
_NON_ANSWER_RECORDS = frozenset(
    {
        "init",
        "user",
        "reasoning",
        "assistant",
        "tool_use",
        "tool_result",
        "approval",
        "compacted",
    }
)


class MegacodeLLMAdapter(CodexCliLLMAdapter):
    """LLM adapter backed by ``harness -p``.

    The harness has no output-schema flag, so structured-output requests are
    enforced cooperatively: the schema goes into the prompt and the extracted
    JSON is validated before it is returned.
    """

    _provider_name = "megacode_cli"
    _display_name = "Megacode harness"
    _default_cli_name = "harness"
    _tempfile_prefix = "ouroboros-megacode-llm-"
    _schema_tempfile_prefix = "ouroboros-megacode-schema-"
    _log_namespace = "megacode_llm_adapter"
    _completion_profile_backend = "megacode"

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
        # The harness has no Codex profile concept; keep the public constructor
        # shape factory-compatible but deliberately ignore runtime_profile.
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

    def _resolve_permission_mode(self, permission_mode: str | None) -> str:
        """Keep the requested mode for bookkeeping; the LLM mode gates nothing."""
        return (permission_mode or "default").strip()

    def _build_permission_args(self) -> list[str]:
        """No permission flags: completions run in the read-only LLM mode."""
        return []

    def _get_configured_cli_path(self) -> str | None:
        """Resolve the harness CLI path from config helpers."""
        return get_megacode_cli_path()

    def _resolve_cli_path(self, cli_path: str | Path | None) -> str:
        """Resolve the harness CLI path from explicit value, config, or PATH."""
        candidate = cli_path or self._get_configured_cli_path()
        if candidate:
            return str(Path(candidate).expanduser())
        return shutil.which(self._default_cli_name) or self._default_cli_name

    def _normalize_model(self, model: str) -> str | None:
        candidate = super()._normalize_model(model)
        if candidate in {"current", "default"}:
            return None
        return candidate

    def _build_prompt(
        self,
        messages: list[Message],
        *,
        max_turns: int | None = None,
        response_format: dict[str, object] | None = None,
    ) -> str:
        prompt = super()._build_prompt(messages, max_turns=max_turns)
        directive = self._build_response_format_directive(response_format)
        if directive:
            prompt = f"{directive}\n\n{prompt}"
        return prompt

    def _build_response_format_directive(
        self,
        response_format: dict[str, object] | None,
    ) -> str | None:
        """Translate response_format into strict prompt instructions."""
        if not response_format:
            return None
        fmt_type = response_format.get("type")
        if fmt_type == "json_schema":
            schema = response_format.get("json_schema")
            if not isinstance(schema, dict):
                return None
            top_type = schema.get("type", "object")
            type_noun = {
                "array": "JSON array",
                "object": "JSON object",
            }.get(str(top_type), "JSON value")
            return (
                f"Respond with ONLY a valid {type_noun} that matches this schema. "
                "Do not use markdown fences, headers, or explanatory text.\n\n"
                f"JSON schema:\n{json.dumps(schema, indent=2, sort_keys=True)}"
            )
        if fmt_type == "json_object":
            return (
                "Respond with ONLY a valid JSON object. Do not use markdown fences, "
                "headers, or explanatory text."
            )
        return None

    def _validate_response_format_payload(
        self,
        payload: str,
        response_format: dict[str, object],
    ) -> str | None:
        """Validate extracted JSON against the requested response_format."""
        return validate_response_format_payload(payload, response_format)

    def _update_last_content(self, last_content: str, event_content: str) -> str:
        """Keep the answer from the ``result`` record."""
        return event_content or last_content

    def _build_output_schema(
        self,
        response_format: dict[str, object] | None,
    ) -> tuple[dict[str, object] | None, tuple[tuple[str, ...], ...]]:
        """The harness has no output-schema flag; enforce cooperatively."""
        if not response_format:
            return None, ()
        log.warning(
            "megacode_llm_adapter.structured_output_soft_enforcement",
            response_format=response_format.get("type"),
            hint=(
                "The harness has no output-schema flag; schema/json requests are "
                "represented in the prompt and validated by callers where applicable."
            ),
        )
        return None, ()

    def _build_command(
        self,
        *,
        output_last_message_path: str,
        output_schema_path: str | None,
        model: str | None,
        profile: str | None = None,
        prompt: str | None = None,
    ) -> list[str]:
        """Build the ``harness -p`` command; the prompt is fed via stdin."""
        del output_last_message_path, output_schema_path, profile, prompt
        command = [
            self._cli_path,
            "-p",
            "--output-format",
            "stream-json",
            "--mode",
            MEGACODE_LLM_MODE,
            "--max-iterations",
            str(max(1, self._max_turns)),
        ]
        if model:
            command.extend(["--model", model])
        return command

    def _extract_text(self, value: object) -> str:
        """Return the answer only from the final ``result`` record."""
        if isinstance(value, dict):
            record_type = value.get("type")
            if record_type in _NON_ANSWER_RECORDS:
                return ""
            if record_type == "result":
                if value.get("is_error"):
                    return ""
                result = value.get("result")
                return result if isinstance(result, str) else ""
        return super()._extract_text(value)

    def _extract_session_id(self, stdout_lines: list[str]) -> str | None:
        for line in stdout_lines:
            event = self._parse_json_event(line)
            if not event:
                continue
            session_id = self._extract_session_id_from_event(event)
            if session_id:
                return session_id
        return None

    def _extract_session_id_from_event(self, event: dict[str, Any]) -> str | None:
        value = event.get("session_id")
        if isinstance(value, str) and value.strip():
            return value.strip()
        return None

    def _extract_stdout_errors(self, stdout_lines: list[str]) -> list[str]:
        errors: list[str] = []
        for line in stdout_lines:
            event = self._parse_json_event(line)
            if not event or event.get("type") != "result" or not event.get("is_error"):
                continue
            error = event.get("error") or f"outcome: {event.get('outcome')}"
            errors.append(str(error))
        return errors

    @classmethod
    def _build_child_env(cls) -> dict[str, str]:
        env = dict(os.environ)
        for key in (
            "OUROBOROS_AGENT_RUNTIME",
            "OUROBOROS_LLM_BACKEND",
            "OUROBOROS_RUNTIME",
            "OUROBOROS_MCP_BRIDGE",
            "OUROBOROS_MCP_BRIDGE_CONFIG",
        ):
            env.pop(key, None)
        env["_OUROBOROS_NESTED"] = "1"
        return env

    def _build_env_for_instance(self) -> dict[str, str]:
        return self._build_child_env()

    def _codex_failure_details(
        self,
        *,
        returncode: int | None,
        session_id: str | None,
        stderr: str,
        stdout_errors: list[str],
        message: str,
        cli_path: str | None = None,
    ) -> dict[str, object]:
        # Keep this hook signature compatible with the shared Codex adapter.
        # The harness has no Codex App/CLI version to inspect.
        del message, cli_path
        return {
            "returncode": returncode,
            "session_id": session_id,
            "stderr": stderr,
            "stdout_errors": stdout_errors,
        }

    async def complete(
        self,
        messages: list[Message],
        config: CompletionConfig,
    ) -> Result[CompletionResponse, ProviderError]:
        """Make a completion request via the harness with JSON extraction support."""
        if not config.response_format:
            return await super().complete(messages, config)

        profile_result = resolve_completion_profile_result(
            config,
            backend=self._completion_profile_backend,
        )
        if profile_result.is_err:
            return Result.err(profile_result.error)
        effective_config = profile_result.value.config

        patched_config = replace(effective_config, response_format=None)
        patched_messages = [*messages]
        directive = self._build_response_format_directive(effective_config.response_format)
        if directive:
            patched_messages.insert(0, Message(role=MessageRole.SYSTEM, content=directive))

        last_result: Result[CompletionResponse, ProviderError] | None = None
        attempts = max(1, self._max_retries)
        for attempt in range(attempts):
            result = await super().complete(patched_messages, patched_config)
            if result.is_err:
                return result
            extracted = extract_json_payload(result.value.content)
            if extracted:
                validation_error = self._validate_response_format_payload(
                    extracted,
                    effective_config.response_format,
                )
                if validation_error is None:
                    return Result.ok(replace(result.value, content=extracted))
                log.warning(
                    "megacode_llm_adapter.response_format_validation_failed",
                    attempt=attempt + 1,
                    max_attempts=attempts,
                    validation_error=validation_error,
                    response_preview=result.value.content[:160],
                )
            else:
                log.warning(
                    "megacode_llm_adapter.json_extraction_failed",
                    attempt=attempt + 1,
                    max_attempts=attempts,
                    response_preview=result.value.content[:160],
                )
            last_result = result

        assert last_result is not None
        return Result.err(
            ProviderError(
                message="JSON format required but the megacode harness returned prose",
                provider=self._provider_name,
                details={"last_response_preview": last_result.value.content[:240]},
            )
        )


__all__ = ["MEGACODE_LLM_MODE", "MegacodeLLMAdapter"]
