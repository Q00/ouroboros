"""Zcode CLI LLM adapter for single-turn completion via a local Zcode session.

Shells out to ``zcode.cjs --prompt --json`` (non-interactive one-shot) — the
same zcode CLI surface :class:`~ouroboros.orchestrator.zcode_cli_runtime.ZcodeCLIRuntime`
uses, measured against ``zcode --help`` on 0.14.5 and 0.15.0. Unlike Codex's
NDJSON event stream, zcode emits ONE buffered JSON summary object on stdout
with top-level ``response`` / ``usage`` / ``sessionId`` fields.

The adapter inherits :class:`CodexCliLLMAdapter`'s prompt building, retry, and
``complete`` scaffolding and overrides only the zcode-specific surface: CLI
command construction (``--prompt --json --mode``), permission mapping, child
env (strips ``OUROBOROS_LLM_BACKEND`` to prevent recursion), and the
single-JSON-summary response parsing.

Model selection: zcode has **no** ``--model`` flag (a hard ``Unknown option``
rejection). The model is selected via ``~/.zcode/cli/config.json``
(``model.main``); any ``model`` passed here is ignored with a warning, matching
``ZcodeCLIRuntime``.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
import json
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import (
    SchemaError as JsonSchemaError,
)
from jsonschema.exceptions import (
    ValidationError as JsonSchemaValidationError,
)
import structlog

from ouroboros.config import get_zcode_cli_path
from ouroboros.core.errors import ProviderError
from ouroboros.core.json_utils import extract_json_payload
from ouroboros.core.security import MAX_LLM_RESPONSE_LENGTH, InputValidator
from ouroboros.core.types import Result
from ouroboros.providers.base import (
    CompletionConfig,
    CompletionResponse,
    Message,
    MessageRole,
    UsageInfo,
)
from ouroboros.providers.codex_cli_adapter import CodexCliLLMAdapter
from ouroboros.runtime.child_env import build_child_env

log = structlog.get_logger(__name__)

# Zcode ``--mode`` mapping. Zcode's permission vocabulary is build/edit/plan/yolo
# (no ``--approval-mode``, no ``--non-interactive`` — ``--prompt`` is already
# non-interactive). Ouroboros' ``"default"`` has no zcode-native equivalent, so
# it is normalized to the safe default (``acceptEdits`` → ``edit``) rather than
# silently escalating. Matches ``ZcodeCLIRuntime``.
_ZCODE_PERMISSION_MODE_TO_FLAG = {
    "acceptEdits": "edit",  # accept edits
    "bypassPermissions": "yolo",  # full bypass
}
_ZCODE_PERMISSION_MODES = frozenset(_ZCODE_PERMISSION_MODE_TO_FLAG)
_ZCODE_DEFAULT_PERMISSION_MODE = "acceptEdits"

#: Maximum Ouroboros nesting depth to prevent fork bombs
_MAX_OUROBOROS_DEPTH = 5
# Child-env strip set for Zcode. Zcode does NOT strip CLAUDECODE (unlike
# codex/copilot/kiro) — preserve that divergence; only the Ouroboros markers
# are removed. ``OUROBOROS_LLM_BACKEND`` MUST be stripped so the zcode child
# does not inherit an LLM-backend override that could route its own nested
# ouroboros calls back into zcode (recursion). Matches ``ZcodeCLIRuntime``.
_CHILD_ENV_STRIP_KEYS = ("OUROBOROS_AGENT_RUNTIME", "OUROBOROS_LLM_BACKEND")


class ZcodeCliLLMAdapter(CodexCliLLMAdapter):
    """LLM adapter backed by the local Zcode CLI (``zcode.cjs --prompt --json``).

    Inherits :class:`CodexCliLLMAdapter`'s prompt-building, retry, and
    ``complete`` scaffolding; overrides the zcode-specific CLI command,
    permission mapping, child env, and the single-JSON-summary response
    parsing (zcode emits one buffered JSON object, not Codex's NDJSON event
    stream).
    """

    _provider_name = "zcode_cli"
    _display_name = "Zcode CLI"
    _default_cli_name = "zcode"
    _tempfile_prefix = "ouroboros-zcode-llm-"
    _schema_tempfile_prefix = "ouroboros-zcode-schema-"
    _log_namespace = "zcode_cli_llm_adapter"
    _completion_profile_backend = "zcode"
    # zcode ``--prompt --json`` buffers its whole summary until completion and
    # stays silent until then — unlike Codex, which streams events continuously.
    # The parent's first-chunk watchdog would cap the ENTIRE task at 60s on any
    # caller that doesn't explicitly disable it, killing healthy long runs as
    # "produced no stdout". Disable at the class level (matches ZcodeCLIRuntime).
    _startup_output_timeout_seconds = None

    def _stream_provider_name(self) -> str:
        return "zcode_cli"

    # -- Permission mode --------------------------------------------------

    def _resolve_permission_mode(self, permission_mode: str | None) -> str:
        """Validate and normalize the Zcode CLI permission mode.

        ``None`` and ``"default"`` resolve to :data:`_ZCODE_DEFAULT_PERMISSION_MODE`
        (``acceptEdits`` → zcode ``--mode edit``). Other recognized Ouroboros
        modes (``acceptEdits``, ``bypassPermissions``) pass through. Anything
        else raises ``ValueError`` rather than silently falling back — fail-open
        on a permission boundary would let a typo escalate the runtime.
        """
        if permission_mode is None:
            return _ZCODE_DEFAULT_PERMISSION_MODE
        candidate = permission_mode.strip()
        if candidate in _ZCODE_PERMISSION_MODES:
            return candidate
        if candidate == "default":
            log.warning(
                "zcode_cli_llm_adapter.permission_mode_coerced",
                requested="default",
                resolved=_ZCODE_DEFAULT_PERMISSION_MODE,
                reason=(
                    "Zcode --mode has no 'default' value (vocabulary is "
                    "build/edit/plan/yolo); normalized to the safe default."
                ),
            )
            return _ZCODE_DEFAULT_PERMISSION_MODE
        msg = (
            f"Unsupported Zcode permission mode: {permission_mode!r} "
            f"(expected one of {sorted(_ZCODE_PERMISSION_MODES)})"
        )
        raise ValueError(msg)

    def _build_permission_args(self) -> list[str]:
        """Return empty list — zcode has no Codex-style permission flags.

        ``--mode`` is added in :meth:`_build_command`.
        """
        return []

    # -- CLI path ---------------------------------------------------------

    def _get_configured_cli_path(self) -> str | None:
        """Resolve an explicit CLI path from config helpers when available.

        Reads from :func:`ouroboros.config.get_zcode_cli_path`, which checks
        ``OUROBOROS_ZCODE_CLI_PATH`` and persisted ``orchestrator.zcode_cli_path``.
        """
        return get_zcode_cli_path()

    def _normalize_model(self, model: str) -> str | None:
        """zcode has no ``--model`` flag — ignore with a warning.

        Model selection lives in ``~/.zcode/cli/config.json`` (``model.main``).
        """
        candidate = (model or "").strip()
        if candidate and candidate != "default":
            log.warning(
                "zcode_cli_llm_adapter.model_not_forwarded",
                requested=model,
                reason=(
                    "zcode has no --model CLI flag; set model.main in "
                    "~/.zcode/cli/config.json to select the model."
                ),
            )
        return None

    # -- Environment ------------------------------------------------------

    def _build_child_env(self) -> dict[str, str]:
        """Build child env with the recursion guard (matches ZcodeCLIRuntime)."""
        return build_child_env(
            strip_keys=_CHILD_ENV_STRIP_KEYS,
            max_depth=_MAX_OUROBOROS_DEPTH,
            depth_error_factory=lambda _depth, max_depth: RuntimeError(
                f"Maximum Ouroboros nesting depth ({max_depth}) exceeded"
            ),
        )

    # -- Command construction --------------------------------------------

    def _build_command(
        self,
        *,
        output_last_message_path: str,
        output_schema_path: str | None,
        model: str | None,
        profile: str | None = None,
        prompt: str | None = None,
    ) -> list[str]:
        """Build the zcode CLI command for a one-shot completion.

        Measured interface (from ``zcode --help``, verified against a live run):
        real flags are ``--prompt`` (one-shot), ``--json`` (machine-readable
        summary), ``--cwd``, ``--mode`` (build|edit|plan|yolo), ``--resume``.

        zcode has **no** ``exec`` subcommand, ``--output-last-message``,
        ``--output-schema``, ``--model``, ``--profile``, ``--ephemeral``, or
        ``--skip-git-repo-check`` — all Codex artifacts. The shared signature
        is honored but the Codex-only args are ignored.

        Two install shapes both work (matches ZcodeCLIRuntime):

        - **App-bundle script** — ``zcode.cjs`` invoked as ``node <cli_path> …``.
        - **PATH executable** — invoked directly (``node <executable>`` would
          parse the binary as JS and fail before zcode runs).

        Distinguished by extension: ``.cjs``/``.js``/``.mjs`` get the ``node``
        prefix; everything else is invoked straight.
        """
        del output_last_message_path, output_schema_path, model, profile

        mode_flag = _ZCODE_PERMISSION_MODE_TO_FLAG.get(
            self._permission_mode,
            "edit",
        )
        cli_path = str(self._cli_path) if self._cli_path else None
        if cli_path is None:
            msg = (
                "zcode CLI path could not be resolved "
                "(set OUROBOROS_ZCODE_CLI_PATH or orchestrator.zcode_cli_path)"
            )
            raise RuntimeError(msg)
        prefix: list[str] = (
            ["node", cli_path] if cli_path.endswith((".cjs", ".js", ".mjs")) else [cli_path]
        )
        command = prefix + [
            "--json",
            "--prompt",
            prompt or "",
            "--mode",
            mode_flag,
        ]
        cwd = getattr(self, "_cwd", None)
        if cwd:
            command.extend(["--cwd", str(cwd)])
        return command

    def _feeds_prompt_via_stdin(self) -> bool:
        """Return False — zcode accepts the prompt via the --prompt flag."""
        return False

    def _requires_process_stdin(self) -> bool:
        """Return False — zcode doesn't need an interactive stdin pipe."""
        return False

    # -- Structured output -----------------------------------------------

    def _build_response_format_directive(
        self,
        response_format: dict[str, object] | None,
    ) -> str | None:
        """Translate response_format into cooperative Zcode prompt instructions.

        Zcode has no Codex-style ``--output-schema`` flag. Preserve the shared
        provider contract by asking for JSON in the prompt, extracting JSON from
        the response, and validating it before returning success.
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
            schema_payload = (
                schema.get("schema") if isinstance(schema.get("schema"), dict) else schema
            )
            top_type = (
                schema_payload.get("type", "object")
                if isinstance(schema_payload, dict)
                else "object"
            )
            type_noun = {"array": "JSON array", "object": "JSON object"}.get(
                str(top_type), "JSON value"
            )
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
            schema_payload = (
                schema.get("schema") if isinstance(schema.get("schema"), dict) else schema
            )
            try:
                Draft202012Validator.check_schema(schema_payload)
                Draft202012Validator(schema_payload).validate(parsed)
            except JsonSchemaError as exc:
                return f"invalid JSON schema: {exc.message}"
            except JsonSchemaValidationError as exc:
                return exc.message
        return None

    # -- Response parsing (single JSON summary) --------------------------

    def _extract_session_id_from_event(self, event: dict[str, Any]) -> str | None:
        """Extract the zcode session id (``sessionId`` of the form ``sess_<uuid>``)."""
        sid = event.get("sessionId")
        if isinstance(sid, str) and sid.strip():
            return sid.strip()
        return None

    @staticmethod
    def _extract_usage(usage: Any) -> UsageInfo:
        """Extract token usage from zcode's ``usage`` summary object."""
        if not isinstance(usage, dict):
            return UsageInfo(prompt_tokens=0, completion_tokens=0, total_tokens=0)
        prompt = int(usage.get("inputTokens") or usage.get("input_tokens") or 0)
        completion = int(usage.get("outputTokens") or usage.get("output_tokens") or 0)
        total = int(usage.get("totalTokens") or usage.get("total_tokens") or (prompt + completion))
        return UsageInfo(
            prompt_tokens=prompt,
            completion_tokens=completion,
            total_tokens=total,
        )

    async def _complete_once(
        self,
        messages: list[Message],
        config: CompletionConfig,
    ) -> Result[CompletionResponse, ProviderError]:
        """Execute a single zcode CLI completion request.

        zcode returns the whole answer as ONE buffered JSON summary on stdout
        (top-level ``response`` / ``usage`` / ``sessionId``). This overrides the
        inherited Codex NDJSON event-stream parsing entirely — the shapes are
        not compatible, and zcode has no ``--output-last-message`` file to read.
        """
        prompt = self._build_prompt(messages, max_turns=getattr(config, "max_turns", None))
        command = self._build_command(
            output_last_message_path="",  # unused by zcode
            output_schema_path=None,
            model=self._normalize_model(config.model),
            profile=None,
            prompt=prompt,
        )
        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                cwd=self._cwd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=self._build_child_env(),
            )
        except FileNotFoundError as exc:
            return Result.err(
                ProviderError(
                    message=f"{self._display_name} not found: {exc}",
                    provider=self._provider_name,
                    details={"cli_path": self._cli_path},
                )
            )
        except Exception as exc:
            return Result.err(
                ProviderError(
                    message=f"Failed to start {self._display_name}: {exc}",
                    provider=self._provider_name,
                    details={
                        "cli_path": self._cli_path,
                        "error_type": type(exc).__name__,
                    },
                )
            )

        if process.stdin is not None:
            process.stdin.close()

        timeout = self._timeout
        try:
            if timeout is None:
                stdout_bytes, stderr_bytes = await process.communicate()
            else:
                async with asyncio.timeout(timeout):
                    stdout_bytes, stderr_bytes = await process.communicate()
        except TimeoutError:
            await self._terminate_process(process)
            return Result.err(
                ProviderError(
                    message=f"{self._display_name} timed out after {timeout}s",
                    provider=self._provider_name,
                    details={"timed_out": True},
                )
            )

        stdout = stdout_bytes.decode("utf-8", errors="replace").strip()
        stderr = stderr_bytes.decode("utf-8", errors="replace").strip()

        if process.returncode != 0:
            return Result.err(
                ProviderError(
                    message=(
                        stderr or f"{self._display_name} exited with code {process.returncode}"
                    ),
                    provider=self._provider_name,
                    details={
                        "returncode": process.returncode,
                        "stderr": stderr,
                        "stdout": stdout[:1000],
                    },
                )
            )

        try:
            event = json.loads(stdout)
        except json.JSONDecodeError:
            return Result.err(
                ProviderError(
                    message=f"{self._display_name} returned non-JSON output",
                    provider=self._provider_name,
                    details={"stdout": stdout[:1000], "stderr": stderr[:1000]},
                )
            )
        if not isinstance(event, dict):
            return Result.err(
                ProviderError(
                    message=f"{self._display_name} returned non-object JSON",
                    provider=self._provider_name,
                    details={"stdout": stdout[:1000]},
                )
            )

        response = event.get("response")
        if not isinstance(response, str) or not response:
            return Result.err(
                ProviderError(
                    message=f"Empty response from {self._display_name}",
                    provider=self._provider_name,
                    details={
                        "session_id": self._extract_session_id_from_event(event),
                    },
                )
            )

        is_valid, _ = InputValidator.validate_llm_response(response)
        if not is_valid:
            log.warning(
                "zcode_cli_llm_adapter.response_truncated",
                original_length=len(response),
                max_length=MAX_LLM_RESPONSE_LENGTH,
            )
            response = response[:MAX_LLM_RESPONSE_LENGTH]

        session_id = self._extract_session_id_from_event(event)
        return Result.ok(
            CompletionResponse(
                content=response,
                model=config.model or "zcode",
                usage=self._extract_usage(event.get("usage")),
                finish_reason="stop",
                raw_response={
                    "session_id": session_id,
                    "returncode": process.returncode,
                    "usage": event.get("usage"),
                    "eventCount": event.get("eventCount"),
                },
            )
        )

    async def complete(
        self,
        messages: list[Message],
        config: CompletionConfig,
    ) -> Result[CompletionResponse, ProviderError]:
        """Make a Zcode completion request, including structured output support."""
        if not config.response_format:
            return await super().complete(messages, config)

        directive = self._build_response_format_directive(config.response_format)
        if not directive:
            return Result.err(
                ProviderError(
                    message="Unsupported Zcode structured response_format request",
                    provider=self._provider_name,
                    details={
                        "response_format_type": config.response_format.get("type"),
                    },
                )
            )

        patched_messages = [Message(role=MessageRole.SYSTEM, content=directive), *messages]
        patched_config = replace(config, response_format=None)
        attempts = max(1, self._max_retries)
        last_response_preview = ""
        for _attempt in range(attempts):
            result = await self._complete_once(patched_messages, patched_config)
            if result.is_err:
                return result
            last_response_preview = result.value.content[:240]
            extracted = extract_json_payload(result.value.content)
            if not extracted:
                continue
            validation_error = self._validate_response_format_payload(
                extracted,
                config.response_format,
            )
            if validation_error is None:
                return Result.ok(replace(result.value, content=extracted))

        return Result.err(
            ProviderError(
                message="JSON format required but Zcode returned non-conforming output",
                provider=self._provider_name,
                details={"last_response_preview": last_response_preview},
            )
        )


__all__ = ["ZcodeCliLLMAdapter"]
