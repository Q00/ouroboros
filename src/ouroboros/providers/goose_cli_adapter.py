"""Goose CLI adapter for LLM completion using local Goose configuration.

This adapter shells out to ``goose run`` in headless mode, allowing Ouroboros
LLM-only calls (interview, planning, evaluation, consensus roles) to use the
same Goose provider/model configuration as the user's Goose session.
"""

from __future__ import annotations

from dataclasses import replace
import json
import os
from pathlib import Path
import shutil
from typing import Any
from uuid import uuid4

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError as JsonSchemaValidationError
import structlog

from ouroboros.config import get_goose_cli_path
from ouroboros.core.errors import ProviderError
from ouroboros.core.json_utils import extract_json_payload
from ouroboros.core.types import Result
from ouroboros.providers.base import CompletionConfig, CompletionResponse, Message, MessageRole
from ouroboros.providers.codex_cli_adapter import CodexCliLLMAdapter
from ouroboros.providers.profiles import resolve_completion_profile_result

log = structlog.get_logger()


def _has_text_payload(value: dict[str, Any]) -> bool:
    for key in ("text", "content", "response", "result", "output", "data", "error", "message"):
        if key not in value:
            continue
        payload = value[key]
        if payload not in (None, "", [], {}):
            return True
    return False


class GooseCliLLMAdapter(CodexCliLLMAdapter):
    """LLM adapter backed by ``goose run``.

    Goose's CLI does not currently provide the same hard sandbox/schema flags
    as Codex CLI. This adapter therefore uses Goose for the model call and
    preserves Ouroboros' unified provider interface, while relying on prompt
    guidance plus post-hoc event parsing for tool envelopes and structured
    output requests.
    """

    _provider_name = "goose_cli"
    _display_name = "Goose CLI"
    _default_cli_name = "goose"
    _tempfile_prefix = "ouroboros-goose-llm-"
    _schema_tempfile_prefix = "ouroboros-goose-schema-"
    _log_namespace = "goose_cli_adapter"
    _completion_profile_backend = "goose"

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
        # Goose has no Codex profile concept; keep the public constructor
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
        """Normalize Goose permission mode values for ``GOOSE_MODE``."""
        candidate = (permission_mode or "approve").strip()
        aliases = {
            "default": "approve",
            "acceptedits": "approve",
            "accept_edits": "approve",
            "bypasspermissions": "auto",
            "bypass_permissions": "auto",
            "auto": "auto",
            "approve": "approve",
            "chat": "chat",
            "smart_approve": "smart_approve",
        }
        return aliases.get(candidate.lower(), candidate)

    def _build_permission_args(self) -> list[str]:
        """Goose permission mode is supplied by ``GOOSE_MODE``."""
        return []

    def _get_configured_cli_path(self) -> str | None:
        """Resolve Goose CLI path from config helpers."""
        return get_goose_cli_path()

    def _resolve_cli_path(self, cli_path: str | Path | None) -> str:
        """Resolve Goose CLI path from explicit value, config, or PATH."""
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
        try:
            parsed = json.loads(payload)
        except json.JSONDecodeError as exc:
            return f"invalid JSON: {exc}"

        fmt_type = response_format.get("type")
        if fmt_type == "json_object":
            if not isinstance(parsed, dict):
                return "expected a JSON object"
            return None

        if fmt_type == "json_schema":
            schema = response_format.get("json_schema")
            if not isinstance(schema, dict):
                return "json_schema response_format is missing a schema object"
            schema_payload = (
                schema.get("schema") if isinstance(schema.get("schema"), dict) else schema
            )
            try:
                Draft202012Validator(schema_payload).validate(parsed)
            except JsonSchemaValidationError as exc:
                return exc.message
        return None

    def _update_last_content(self, last_content: str, event_content: str) -> str:
        """Accumulate Goose stream chunks for completion fallback."""
        if not event_content:
            return last_content
        if last_content and event_content.startswith(last_content):
            return event_content
        return f"{last_content}{event_content}"

    def _build_output_schema(
        self,
        response_format: dict[str, object] | None,
    ) -> tuple[dict[str, object] | None, tuple[tuple[str, ...], ...]]:
        """Goose CLI has no hard JSON-schema flag; enforce cooperatively."""
        if not response_format:
            return None, ()
        log.warning(
            "goose_cli_adapter.structured_output_soft_enforcement",
            response_format=response_format.get("type"),
            hint=(
                "Goose CLI has no output-schema flag; schema/json requests are "
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
    ) -> list[str]:
        """Build the ``goose run`` command; prompt is fed via stdin."""
        del output_last_message_path, output_schema_path, profile
        command = [self._cli_path, "run", "--output-format", "stream-json"]
        if self._ephemeral:
            command.append("--no-session")
        else:
            command.extend(["--name", f"ouroboros-llm-{uuid4().hex[:12]}"])
        command.extend(["--max-turns", str(max(1, self._max_turns)), "-i", "-"])
        if model:
            command.extend(["--model", model])
        return command

    def _extract_text(self, value: object) -> str:
        """Extract assistant text from Goose stream-json events.

        Goose emits final bookkeeping events such as ``{"type":"complete"}``
        whose numeric fields should not become the completion content. Prefer
        user-visible assistant message payloads and ignore completion metadata.
        """
        if isinstance(value, dict):
            event_type = value.get("type")
            if event_type in {"init", "session", "session.started", "session.created"}:
                return ""
            if event_type in {"complete", "completed", "done"} and not _has_text_payload(value):
                return ""
            if event_type == "message" and isinstance(value.get("message"), dict):
                return self._extract_text(value["message"])
            content = value.get("content")
            if isinstance(content, str):
                return content
            for key in ("text", "response", "result", "output", "data", "error"):
                payload = value.get(key)
                if isinstance(payload, str):
                    return payload
                if isinstance(payload, dict | list):
                    text = self._extract_text(payload)
                    if text:
                        return text
            if isinstance(content, list):
                parts: list[str] = []
                for item in content:
                    if isinstance(item, dict):
                        if item.get("type") == "text" and isinstance(item.get("text"), str):
                            parts.append(item["text"])
                        else:
                            text = self._extract_text(item)
                            if text:
                                parts.append(text)
                    elif isinstance(item, str):
                        parts.append(item)
                return "".join(parts)
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
        for key in ("session_id", "sessionId"):
            value = event.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        session = event.get("session")
        if isinstance(session, dict):
            value = session.get("id") or session.get("session_id")
            if isinstance(value, str) and value.strip():
                return value.strip()
        return None

    def _extract_stdout_errors(self, stdout_lines: list[str]) -> list[str]:
        errors: list[str] = []
        for line in stdout_lines:
            event = self._parse_json_event(line)
            if not event:
                continue
            event_type = event.get("type")
            if event_type not in {"error", "turn.failed"}:
                continue
            text = self._extract_text(event.get("error") or event)
            if text:
                errors.append(text)
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
        env = self._build_child_env()
        env["GOOSE_MODE"] = self._permission_mode
        if self._cwd:
            env["GOOSE_WORKING_DIR"] = self._cwd
        return env

    def _codex_failure_details(
        self,
        *,
        returncode: int | None,
        session_id: str | None,
        stderr: str,
        stdout_errors: list[str],
        message: str,
    ) -> dict[str, object]:
        del message
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
        """Make a completion request via Goose CLI with JSON extraction support."""
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
                    "goose_cli_adapter.response_format_validation_failed",
                    attempt=attempt + 1,
                    max_attempts=attempts,
                    validation_error=validation_error,
                    response_preview=result.value.content[:160],
                )
            else:
                log.warning(
                    "goose_cli_adapter.json_extraction_failed",
                    attempt=attempt + 1,
                    max_attempts=attempts,
                    response_preview=result.value.content[:160],
                )
            last_result = result

        assert last_result is not None
        return Result.err(
            ProviderError(
                message="JSON format required but Goose returned prose",
                provider=self._provider_name,
                details={"last_response_preview": last_result.value.content[:240]},
            )
        )


__all__ = ["GooseCliLLMAdapter"]
