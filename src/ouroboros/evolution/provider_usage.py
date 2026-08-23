"""Fail-closed token accounting for non-executor evolution provider calls.

The AC executor already persists one token receipt per primary runtime attempt.
Wonder, Reflect, validation repair, assertion extraction, and semantic/consensus
evaluation use different provider interfaces, so their calls are collected in a
generation-local context and folded into the same paired frugality receipt.

Only runtime-reported usage is accepted.  A provider error, exception, malformed
counter, or zero/absent usage makes the whole generation incomplete rather than
letting a partial numerator manufacture savings.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from functools import wraps
import hashlib
import json
import math
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urlsplit

from ouroboros.core.types import Result
from ouroboros.providers.base import CompletionConfig
from ouroboros.providers.frugality_attestation import PreparedFrugalityCompletion

_MAX_PROVIDER_CALLS = 2_000
_MAX_CONFIGURATION_DEPTH = 16
_MAX_CONFIGURATION_NODES = 4_096
_MAX_CONFIGURATION_TEXT_LENGTH = 16_384
_INVALID_CONFIGURATION = object()
_SENSITIVE_CONFIGURATION_KEYS = frozenset(
    {
        "access_token",
        "api_key",
        "apikey",
        "authorization",
        "client_secret",
        "credential",
        "credentials",
        "password",
        "private_key",
        "refresh_token",
        "secret",
    }
)
_COMPLETION_CONTRACT_KEYS = frozenset(
    {
        "schema_version",
        "schema_id",
        "internal_attempt_limit",
        "credential_authority",
        "effective_request",
        "settings",
    }
)
_AGENT_RUNTIME_CONTRACT_KEYS = frozenset(
    {
        "schema_version",
        "schema_id",
        "implementation",
        "runtime_backend",
        "runtime_handle_backend",
        "settings",
    }
)
_EFFECTIVE_REQUEST_KEYS = frozenset(
    {
        "model",
        "profile_model",
        "temperature",
        "max_tokens",
        "stop",
        "top_p",
        "response_format",
        "role",
        "profile",
        "profile_name",
        "backend_profile",
        "max_turns",
        "reasoning_effort",
        "model_is_explicit",
    }
)
_SCOPED_HANDLE_METADATA_FIELDS = frozenset(
    {
        "execution_id",
        "level_number",
        "scope",
        "server_session_id",
        "session_role",
        "session_scope_id",
        "session_state_path",
    }
)


@dataclass(frozen=True, slots=True)
class _CompletionAdapterSchema:
    schema_id: str
    settings: frozenset[str]
    retry_setting: str
    proof_retry_value: int
    schema_version: int = 1
    requires_sealed_dispatch: bool = False


_COMPLETION_ADAPTER_SCHEMAS: dict[str, _CompletionAdapterSchema] = {
    "ouroboros.providers.litellm_adapter.LiteLLMAdapter": _CompletionAdapterSchema(
        schema_id="ouroboros.litellm.v7",
        settings=frozenset(
            {
                "api_base",
                "timeout",
                "max_retries",
                "io_recorder_configured",
                "dependency_version",
                "dependency_provider",
                "dependency_defaults_verified",
                "dependency_max_retries",
                "dependency_client_sealed",
                "dependency_client_type",
                "dependency_transport_contract",
                "dependency_cleanup_required",
                "dependency_isolation_kind",
                "dependency_worker_protocol",
                "dependency_worker_environment_contract",
                "dependency_worker_cwd_contract",
                "dependency_mutation_audit",
                "worker_launcher_path",
                "worker_executable_resolved_path",
                "worker_executable_content_sha256",
                "worker_python_version",
                "worker_module",
                "worker_implementation_sha256",
                "adapter_module",
                "adapter_implementation_sha256",
                "ouroboros_implementation_contract",
                "ouroboros_implementation_sha256",
                "httpx_dependency_version",
                "openai_dependency_version",
            }
        ),
        retry_setting="max_retries",
        proof_retry_value=1,
        schema_version=7,
        requires_sealed_dispatch=True,
    ),
    "ouroboros.providers.anthropic_adapter.AnthropicAdapter": _CompletionAdapterSchema(
        schema_id="ouroboros.anthropic.v2",
        settings=frozenset(
            {
                "api_base",
                "timeout",
                "max_retries",
                "default_model",
                "io_recorder_configured",
            }
        ),
        retry_setting="max_retries",
        proof_retry_value=0,
        schema_version=2,
    ),
}


@dataclass(frozen=True, slots=True)
class _AgentRuntimeSchema:
    schema_id: str
    runtime_backend: str
    runtime_handle_backend: str
    setting_kinds: Mapping[str, str]
    schema_version: int = 1
    literal_settings: Mapping[str, frozenset[str]] = field(default_factory=dict)


_AGENT_RUNTIME_SCHEMAS: dict[str, _AgentRuntimeSchema] = {
    "ouroboros.orchestrator.codex_cli_runtime.CodexCliRuntime": _AgentRuntimeSchema(
        schema_id="ouroboros.codex_cli_runtime.v4",
        runtime_backend="codex",
        runtime_handle_backend="codex_cli",
        schema_version=4,
        setting_kinds={
            "provider_name": "text",
            "llm_backend": "text",
            "permission_mode": "text",
            "constructor_model": "optional_text",
            "runtime_profile": "optional_text",
            "codex_profile": "optional_text",
            "resolved_model": "optional_text",
            "resolved_profile": "optional_text",
            "resolved_agent": "optional_text",
            "resolved_reasoning_effort": "optional_text",
            "semantic_cwd": "relative_posix_path",
            "cli_path": "absolute_path",
            "cli_executable_path": "absolute_path",
            "cli_executable_content_sha256": "sha256",
            "cli_executable_version_identity": "sha256",
            "skills_source": "text",
            "skills_location": "skill_location",
            "skill_dispatcher_kind": "text",
            "skill_dispatcher_identity": "dispatcher_identity",
            "skill_dispatch_registry_fingerprint": "sha256",
            "builtin_mcp_handler_registry_fingerprint": "sha256",
            "startup_output_timeout_seconds": "optional_positive_number",
            "stdout_idle_timeout_seconds": "optional_positive_number",
            "process_shutdown_timeout_seconds": "positive_number",
            "completed_process_group_shutdown_timeout_seconds": "positive_number",
            "max_resume_retries": "positive_int",
            "max_ouroboros_depth": "positive_int",
            "max_stderr_lines": "positive_int",
            "use_process_group": "bool",
            "child_session_env_keys": "string_sequence",
            "child_environment_authority_fingerprint": "sha256",
            "child_environment_authority_scheme": "text",
            "child_environment_entry_count": "positive_int",
            "child_environment_sensitive_entry_count": "non_negative_int",
            "profile_resolution_fingerprint": "sha256",
            "codex_config_authority_fingerprint": "sha256",
            "codex_config_authority_scheme": "text",
            "codex_profile_v2_names": "string_sequence",
            "codex_project_trust_paths": "string_sequence",
        },
        literal_settings={
            "skills_source": frozenset({"directory", "package"}),
            "skill_dispatcher_kind": frozenset({"custom", "packaged"}),
            "child_environment_authority_scheme": frozenset({"hmac-sha256-installation-key-v2"}),
            "codex_config_authority_scheme": frozenset({"hmac-sha256-installation-key-v2"}),
        },
    ),
}


@dataclass(frozen=True, slots=True)
class ProviderUsageSummary:
    """Generation-level provider-call token evidence."""

    call_count: int
    token_spend: float | None
    identity_digest: str | None
    configuration_fingerprint: str | None
    configuration_assignments: tuple[tuple[str, tuple[str, ...]], ...]
    complete: bool
    issues: tuple[str, ...] = ()


@dataclass(slots=True)
class _ProviderCall:
    call_id: str
    role: str
    model: str
    configuration: str
    token_spend: float | None = None
    finished: bool = False


@dataclass(slots=True)
class GenerationProviderUsageCapture:
    """Mutable collector scoped to one evolution generation task."""

    _calls: list[_ProviderCall] = field(default_factory=list)
    _overflowed_call_count: int = 0
    instrumentation_complete: bool = True

    def begin(
        self,
        *,
        role: str | None,
        model: str | None,
        configuration: Mapping[str, object],
    ) -> _ProviderCall:
        normalized_role = role.strip() if isinstance(role, str) and role.strip() else "unknown"
        normalized_model = model.strip() if isinstance(model, str) and model.strip() else "unknown"
        try:
            normalized_configuration = _canonical_configuration(configuration)
        except (TypeError, ValueError, RecursionError):
            normalized_configuration = "invalid"
            self.instrumentation_complete = False
        call_index = len(self._calls) + self._overflowed_call_count
        call = _ProviderCall(
            call_id=f"provider:{call_index}",
            role=normalized_role,
            model=normalized_model,
            configuration=normalized_configuration,
        )
        if normalized_role.lower() == "unknown":
            self.instrumentation_complete = False
        if normalized_model.lower() == "unknown":
            self.instrumentation_complete = False
        if len(self._calls) >= _MAX_PROVIDER_CALLS:
            self._overflowed_call_count += 1
            self.instrumentation_complete = False
        else:
            self._calls.append(call)
        return call

    @staticmethod
    def finish(call: _ProviderCall, token_spend: float | None) -> None:
        call.token_spend = token_spend
        call.finished = True

    def resolve_model(self, call: _ProviderCall, model: object) -> None:
        resolved = model.strip() if isinstance(model, str) and model.strip() else None
        if resolved is None:
            self.instrumentation_complete = False
            return
        call.model = resolved
        if resolved.lower() == "unknown":
            self.instrumentation_complete = False
            return
        try:
            configuration = json.loads(call.configuration)
        except (TypeError, json.JSONDecodeError):
            return
        if isinstance(configuration, dict):
            configuration["resolved_model"] = resolved
            call.configuration = _canonical_configuration(configuration)

    def observe_callable(self, callable_obj: Any) -> None:
        owner = getattr(callable_obj, "__self__", None)
        if not any(
            getattr(candidate, "frugality_provider_tracking", False) is True
            for candidate in (callable_obj, owner)
            if candidate is not None
        ):
            self.instrumentation_complete = False

    def mark_incomplete(self) -> None:
        self.instrumentation_complete = False

    def summary(self, *, instrumentation_complete: bool | None = None) -> ProviderUsageSummary:
        complete_instrumentation = (
            self.instrumentation_complete
            if instrumentation_complete is None
            else instrumentation_complete and self.instrumentation_complete
        )
        issues: list[str] = []
        if not complete_instrumentation:
            issues.append("generation provider-call instrumentation is incomplete")
        if self._overflowed_call_count:
            issues.append("provider call evidence exceeded the bounded collection limit")
        incomplete = [call.call_id for call in self._calls if not call.finished]
        missing_usage = [
            call.call_id
            for call in self._calls
            if call.finished and _positive_finite(call.token_spend) is None
        ]
        if incomplete:
            issues.append("provider calls did not reach a measured terminal result")
        if missing_usage:
            issues.append("provider runtime-token usage is missing or invalid")

        identities = [
            {
                "call_id": call.call_id,
                "role": call.role,
                "model": call.model,
                "configuration": call.configuration,
            }
            for call in self._calls
        ]
        encoded = json.dumps(identities, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        digest = "sha256:" + hashlib.sha256(encoded.encode()).hexdigest()
        configuration_fingerprint = _fingerprint_configurations(
            call.configuration for call in self._calls
        )
        configuration_assignments = _configuration_assignments(self._calls)
        complete = (
            complete_instrumentation
            and not self._overflowed_call_count
            and not incomplete
            and not missing_usage
        )
        token_spend: float | None = None
        if complete:
            candidate_spend = sum(float(call.token_spend) for call in self._calls)
            token_spend = _finite_non_negative(candidate_spend)
            if token_spend is None:
                complete = False
                issues.append("provider runtime-token total is non-finite")
        return ProviderUsageSummary(
            call_count=len(self._calls) + self._overflowed_call_count,
            token_spend=token_spend,
            identity_digest=digest,
            configuration_fingerprint=configuration_fingerprint,
            configuration_assignments=configuration_assignments,
            complete=complete,
            issues=tuple(issues),
        )


_ACTIVE_CAPTURE: ContextVar[GenerationProviderUsageCapture | None] = ContextVar(
    "ouroboros_evolution_provider_usage_capture",
    default=None,
)


@contextmanager
def capture_generation_provider_usage() -> Iterator[GenerationProviderUsageCapture]:
    """Install one task-local provider usage collector."""
    capture = GenerationProviderUsageCapture()
    token = _ACTIVE_CAPTURE.set(capture)
    try:
        yield capture
    finally:
        _ACTIVE_CAPTURE.reset(token)


def capture_provider_usage(callable_obj: Any) -> Any:
    """Decorate one generation coroutine with a task-local usage capture."""

    @wraps(callable_obj)
    async def _wrapped(*args: Any, **kwargs: Any) -> Any:
        with capture_generation_provider_usage() as capture:
            if kwargs.get("resume_after_phase") is not None:
                capture.mark_incomplete()
            return await callable_obj(*args, **kwargs)

    return _wrapped


def observe_callable(callable_obj: Any) -> None:
    capture = _ACTIVE_CAPTURE.get()
    if capture is not None:
        capture.observe_callable(callable_obj)


async def call(callable_obj: Any, *args: Any, **kwargs: Any) -> Any:
    """Mark an opaque phase incomplete unless its callable owns all call tracking."""
    observe_callable(callable_obj)
    return await callable_obj(*args, **kwargs)


def current_summary() -> ProviderUsageSummary | None:
    capture = _ACTIVE_CAPTURE.get()
    return capture.summary() if capture is not None else None


async def tracked_complete(
    adapter: Any,
    messages: Any,
    config: Any,
) -> Any:
    """Call an LLM adapter and account for its runtime-reported token usage."""
    capture = _ACTIVE_CAPTURE.get()
    if capture is None:
        return await adapter.complete(messages, config)

    (
        effective_config,
        adapter_contract,
        preparation_error,
        preparation_complete,
        prepared,
    ) = _prepare_completion_configuration(adapter, config)
    if not preparation_complete:
        capture.mark_incomplete()

    call = capture.begin(
        role=getattr(config, "role", None),
        model=getattr(effective_config, "model", None),
        configuration=_completion_configuration(
            adapter,
            config,
            effective_config,
            adapter_contract,
        ),
    )
    if preparation_error is not None:
        capture.finish(call, None)
        return preparation_error
    if prepared is not None and not _prepared_completion_authority_is_current(adapter, prepared):
        capture.mark_incomplete()
    try:
        prepared_dispatch = getattr(adapter, "frugality_complete_prepared", None)
        if prepared is not None and callable(prepared_dispatch):
            result = await prepared_dispatch(messages, prepared)
        else:
            result = await adapter.complete(messages, effective_config)
    except BaseException:
        if prepared is not None and not _prepared_completion_authority_is_current(
            adapter, prepared
        ):
            capture.mark_incomplete()
        capture.finish(call, None)
        raise

    if prepared is not None and not _prepared_completion_authority_is_current(adapter, prepared):
        capture.mark_incomplete()

    token_spend: float | None = None
    if getattr(result, "is_ok", False):
        response = getattr(result, "value", None)
        capture.resolve_model(call, getattr(response, "model", None))
        usage = getattr(response, "usage", None)
        counters = [
            *(
                getattr(usage, field, None)
                for field in ("prompt_tokens", "completion_tokens", "total_tokens")
            ),
            getattr(usage, "unallocated_tokens", 0),
        ]
        normalized_counters = tuple(_non_negative_counter(counter) for counter in counters)
        if all(counter is not None for counter in normalized_counters):
            prompt_tokens, completion_tokens, total_tokens, unallocated_tokens = normalized_counters
            if (
                prompt_tokens is not None
                and completion_tokens is not None
                and total_tokens is not None
                and unallocated_tokens is not None
                and total_tokens == prompt_tokens + completion_tokens
            ):
                token_spend = _positive_finite(total_tokens + unallocated_tokens)
    capture.finish(call, token_spend)
    return result


def _prepared_completion_authority_is_current(
    adapter: Any,
    prepared: PreparedFrugalityCompletion,
) -> bool:
    """Treat mutable provider-authority drift as incomplete telemetry only."""
    validator = getattr(adapter, "frugality_validate_prepared_completion", None)
    if not callable(validator):
        return True
    try:
        return validator(prepared) is True
    except Exception:  # noqa: BLE001 — opaque dependency authority fails closed
        return False


async def tracked_agent_task_to_result(
    runtime: Any,
    *,
    role: str,
    prompt: str,
    tools: list[str] | None = None,
    system_prompt: str | None = None,
) -> Any:
    """Call an agent runtime and strictly sum usage across its message stream."""
    capture = _ACTIVE_CAPTURE.get()
    if capture is None:
        return await runtime.execute_task_to_result(
            prompt=prompt,
            tools=tools,
            system_prompt=system_prompt,
        )

    call = capture.begin(
        role=role,
        model=_runtime_setting(runtime, "model", "_model"),
        configuration=_agent_runtime_configuration(
            runtime,
            {},
            tools=tools,
            system_prompt=system_prompt,
        ),
    )
    try:
        result = await runtime.execute_task_to_result(
            prompt=prompt,
            tools=tools,
            system_prompt=system_prompt,
        )
    except BaseException:
        capture.finish(call, None)
        _complete_agent_runtime_attestation(runtime, capture)
        raise

    token_spend: float | None = None
    if getattr(result, "is_ok", False):
        task_result = getattr(result, "value", None)
        try:
            messages = tuple(getattr(task_result, "messages", ()))
        except TypeError:
            messages = ()
            capture.mark_incomplete()
        _bind_stream_effective_model(capture, call, messages)
        token_spend = _message_token_spend(messages)
    capture.finish(call, token_spend)
    _complete_agent_runtime_attestation(runtime, capture)
    return result


async def tracked_agent_task(
    runtime: Any,
    *,
    role: str,
    prompt: str,
    tools: list[str] | None = None,
    system_prompt: str | None = None,
    **kwargs: Any,
) -> AsyncIterator[Any]:
    """Stream one auxiliary runtime call while accounting all reported usage."""
    capture = _ACTIVE_CAPTURE.get()
    if capture is None:
        async for message in runtime.execute_task(
            prompt=prompt,
            tools=tools,
            system_prompt=system_prompt,
            **kwargs,
        ):
            yield message
        return

    call = capture.begin(
        role=role,
        model=_runtime_setting(runtime, "model", "_model"),
        configuration=_agent_runtime_configuration(
            runtime,
            kwargs,
            tools=tools,
            system_prompt=system_prompt,
        ),
    )
    messages: list[Any] = []
    try:
        async for message in runtime.execute_task(
            prompt=prompt,
            tools=tools,
            system_prompt=system_prompt,
            **kwargs,
        ):
            messages.append(message)
            yield message
    except BaseException:
        capture.finish(call, None)
        _complete_agent_runtime_attestation(runtime, capture)
        raise
    _bind_stream_effective_model(capture, call, messages)
    capture.finish(call, _message_token_spend(messages))
    _complete_agent_runtime_attestation(runtime, capture)


def _complete_agent_runtime_attestation(
    runtime: Any,
    capture: GenerationProviderUsageCapture,
) -> None:
    """Release task-local runtime authority without failing evolution."""
    complete = getattr(runtime, "frugality_runtime_attestation_complete", None)
    if not callable(complete):
        return
    try:
        complete()
    except Exception:  # noqa: BLE001 - cleanup failure invalidates only proof
        capture.mark_incomplete()


def _runtime_setting(runtime: Any, *names: str) -> str | None:
    for name in names:
        value = getattr(runtime, name, None)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _runtime_configuration_setting(runtime: Any, *names: str) -> object:
    """Return the first declared value without erasing explicit blanks."""
    for name in names:
        value = getattr(runtime, name, None)
        if value is not None:
            return value
    return None


def _prepare_completion_configuration(
    adapter: Any,
    config: Any,
) -> tuple[
    Any,
    object,
    Result[Any, Any] | None,
    bool,
    PreparedFrugalityCompletion | None,
]:
    """Seal profile resolution before fingerprinting and provider dispatch.

    Eligible adapters resolve their role/profile exactly once and return a
    config with those selectors cleared.  Passing that sealed config through
    ``complete`` prevents the adapter from resolving a different global
    profile after evidence has already been captured.
    """
    provider = getattr(adapter, "frugality_prepare_completion", None)
    if not callable(provider):
        return config, _INVALID_CONFIGURATION, None, False, None
    try:
        prepared_result = provider(config)
    except Exception:  # noqa: BLE001 — opaque preparation must fail closed
        return config, _INVALID_CONFIGURATION, None, False, None
    if not isinstance(prepared_result, Result):
        return config, _INVALID_CONFIGURATION, None, False, None
    if prepared_result.is_err:
        return config, _INVALID_CONFIGURATION, prepared_result, False, None
    prepared = prepared_result.value
    if (
        not isinstance(prepared, PreparedFrugalityCompletion)
        or not isinstance(prepared.config, CompletionConfig)
        or prepared.config.role is not None
        or prepared.config.profile is not None
    ):
        return config, _INVALID_CONFIGURATION, None, False, None
    adapter_class = f"{type(adapter).__module__}.{type(adapter).__qualname__}"
    schema = _COMPLETION_ADAPTER_SCHEMAS.get(adapter_class)
    if schema is not None and schema.requires_sealed_dispatch:
        dispatch = getattr(adapter, "frugality_complete_prepared", None)
        if prepared.dispatch is None or not callable(dispatch):
            return prepared.config, prepared.contract, None, False, prepared
    return prepared.config, prepared.contract, None, True, prepared


def _completion_configuration(
    adapter: Any,
    requested_config: Any,
    effective_config: Any,
    adapter_contract: object,
) -> dict[str, object]:
    return {
        "surface": "completion",
        "backend": _completion_backend_configuration(adapter),
        "adapter_contract": _completion_adapter_configuration(adapter, adapter_contract),
        "requested_model": _configuration_text(getattr(requested_config, "model", None)),
        "requested_profile": _configuration_text(
            getattr(requested_config, "profile", None),
            optional=True,
        ),
        "model": _configuration_text(getattr(effective_config, "model", None)),
        "reasoning_effort": _configuration_text(
            getattr(effective_config, "reasoning_effort", None),
            optional=True,
        ),
        "temperature": getattr(effective_config, "temperature", None),
        "top_p": getattr(effective_config, "top_p", None),
        "max_tokens": getattr(effective_config, "max_tokens", None),
        "max_turns": getattr(effective_config, "max_turns", None),
        "model_is_explicit": getattr(effective_config, "model_is_explicit", None),
        "response_format": getattr(effective_config, "response_format", None),
        "stop": getattr(effective_config, "stop", None),
    }


def _completion_backend_configuration(adapter: Any) -> object:
    """Preserve an explicit blank/unknown backend as invalid, not absent."""
    for name in ("llm_backend", "runtime_backend", "provider"):
        value = getattr(adapter, name, None)
        if value is not None:
            return _configuration_text(value)
    return _configuration_text(f"{type(adapter).__module__}.{type(adapter).__qualname__}")


def _completion_adapter_configuration(adapter: Any, contract: object) -> object:
    """Validate one registered, resolved, single-attempt execution envelope."""
    adapter_class = f"{type(adapter).__module__}.{type(adapter).__qualname__}"
    schema = _COMPLETION_ADAPTER_SCHEMAS.get(adapter_class)
    if schema is None:
        return _INVALID_CONFIGURATION
    try:
        if not _valid_completion_adapter_contract(contract, schema):
            return _INVALID_CONFIGURATION
        assert isinstance(contract, Mapping)
        settings = contract["settings"]
        effective_request = contract["effective_request"]
        assert isinstance(settings, Mapping)
        assert isinstance(effective_request, Mapping)
        return {
            "adapter_class": adapter_class,
            "schema_version": schema.schema_version,
            "schema_id": schema.schema_id,
            "internal_attempt_limit": 1,
            "credential_authority": contract["credential_authority"],
            "effective_request": dict(effective_request),
            "settings": dict(settings),
        }
    except Exception:  # noqa: BLE001 — opaque attestation must fail closed
        return _INVALID_CONFIGURATION


def _valid_completion_adapter_contract(
    contract: object,
    schema: _CompletionAdapterSchema,
) -> bool:
    if not isinstance(contract, Mapping):
        return False
    if not _json_primitive_tree(contract, reject_sensitive_keys=True):
        return False
    if set(contract) != _COMPLETION_CONTRACT_KEYS:
        return False
    schema_version = contract.get("schema_version")
    if (
        type(schema_version) is not int
        or schema_version != schema.schema_version
        or contract.get("schema_id") != schema.schema_id
    ):
        return False
    attempt_limit = contract.get("internal_attempt_limit")
    if type(attempt_limit) is not int or attempt_limit != 1:
        return False
    if not _valid_credential_authority(contract.get("credential_authority")):
        return False

    settings = contract.get("settings")
    if not isinstance(settings, Mapping) or not settings or set(settings) != schema.settings:
        return False
    retry_value = settings.get(schema.retry_setting)
    if type(retry_value) is not int or retry_value != schema.proof_retry_value:
        return False
    if not _valid_registered_settings(settings):
        return False

    return _valid_effective_request(contract.get("effective_request"))


def _valid_credential_authority(value: object) -> bool:
    prefix = "hmac-sha256-installation-key-v2:"
    if not isinstance(value, str) or not value.startswith(prefix):
        return False
    digest = value.removeprefix(prefix)
    return len(digest) == 64 and all(character in "0123456789abcdef" for character in digest)


def _valid_registered_settings(settings: Mapping[str, object]) -> bool:
    if _positive_finite(settings.get("timeout")) is None:
        return False
    if not isinstance(settings.get("io_recorder_configured"), bool):
        return False
    api_base = settings.get("api_base")
    if not _valid_api_base(api_base):
        return False
    if "dependency_defaults_verified" in settings and (
        settings.get("dependency_defaults_verified") is not True
        or type(settings.get("dependency_max_retries")) is not int
        or settings.get("dependency_max_retries") != 0
        or _configuration_text(settings.get("dependency_version")) is _INVALID_CONFIGURATION
        or settings.get("dependency_provider")
        not in {"anthropic", "google", "openai", "openrouter"}
        or settings.get("dependency_client_sealed") is not True
        or settings.get("dependency_cleanup_required") is not True
        or settings.get("dependency_transport_contract") != "ouroboros.httpx.async.v1"
        or settings.get("dependency_isolation_kind") != "subprocess"
        or settings.get("dependency_worker_protocol") != "ouroboros.litellm.worker.v1"
        or settings.get("dependency_worker_environment_contract") != "ouroboros.empty-env.v1"
        or settings.get("dependency_worker_cwd_contract") != "ouroboros.private-empty-tempdir.v1"
        or settings.get("dependency_mutation_audit") != "litellm.module-setattr.v1"
        or _configuration_text(settings.get("worker_launcher_path")) is _INVALID_CONFIGURATION
        or not isinstance(settings.get("worker_launcher_path"), str)
        or not Path(settings["worker_launcher_path"]).is_absolute()
        or _configuration_text(settings.get("worker_executable_resolved_path"))
        is _INVALID_CONFIGURATION
        or not isinstance(settings.get("worker_executable_resolved_path"), str)
        or not Path(settings["worker_executable_resolved_path"]).is_absolute()
        or not _valid_sha256(settings.get("worker_executable_content_sha256"))
        or _configuration_text(settings.get("worker_python_version")) is _INVALID_CONFIGURATION
        or settings.get("worker_module") != "ouroboros.providers.litellm_proof_worker"
        or not _valid_sha256(settings.get("worker_implementation_sha256"))
        or settings.get("adapter_module") != "ouroboros.providers.litellm_adapter"
        or not _valid_sha256(settings.get("adapter_implementation_sha256"))
        or settings.get("ouroboros_implementation_contract") != "ouroboros.python-sources.v1"
        or not _valid_sha256(settings.get("ouroboros_implementation_sha256"))
        or _configuration_text(settings.get("httpx_dependency_version")) is _INVALID_CONFIGURATION
        or _configuration_text(settings.get("openai_dependency_version")) is _INVALID_CONFIGURATION
        or settings.get("dependency_client_type")
        != (
            "openai.AsyncOpenAI"
            if settings.get("dependency_provider") == "openai"
            else "litellm.AsyncHTTPHandler"
        )
    ):
        return False
    default_model = settings.get("default_model")
    return not (
        default_model is not None and _configuration_text(default_model) is _INVALID_CONFIGURATION
    )


def _valid_api_base(value: object) -> bool:
    """Accept only explicit, credential-free HTTP(S) route authority."""
    if _configuration_text(value) is _INVALID_CONFIGURATION or not isinstance(value, str):
        return False
    try:
        parsed = urlsplit(value)
        _ = parsed.port
    except ValueError:
        return False
    return bool(
        parsed.scheme in {"http", "https"}
        and parsed.hostname
        and parsed.username is None
        and parsed.password is None
        and not parsed.query
        and not parsed.fragment
    )


def _valid_effective_request(value: object) -> bool:
    if not isinstance(value, Mapping) or set(value) != _EFFECTIVE_REQUEST_KEYS:
        return False
    for key in ("model", "profile_model", "role"):
        if _configuration_text(value.get(key)) is _INVALID_CONFIGURATION:
            return False
    for key in ("profile", "profile_name", "backend_profile", "reasoning_effort"):
        candidate = value.get(key)
        if candidate is not None and _configuration_text(candidate) is _INVALID_CONFIGURATION:
            return False
    max_tokens = value.get("max_tokens")
    if type(max_tokens) is not int or _positive_finite(max_tokens) is None:
        return False
    if _finite_non_negative(value.get("temperature")) is None:
        return False
    if _finite_non_negative(value.get("top_p")) is None:
        return False
    max_turns = value.get("max_turns")
    if max_turns is not None and (
        type(max_turns) is not int or _positive_finite(max_turns) is None
    ):
        return False
    if not isinstance(value.get("model_is_explicit"), bool):
        return False
    stop = value.get("stop")
    if stop is not None and (
        not isinstance(stop, list) or any(not isinstance(item, str) for item in stop)
    ):
        return False
    response_format = value.get("response_format")
    return response_format is None or isinstance(response_format, Mapping)


def _agent_runtime_attestation_configuration(runtime: Any) -> object:
    """Validate one exact, registered runtime-owned execution envelope."""
    runtime_class = f"{type(runtime).__module__}.{type(runtime).__qualname__}"
    schema = _AGENT_RUNTIME_SCHEMAS.get(runtime_class)
    provider = getattr(runtime, "frugality_runtime_attestation", None)
    if schema is None or not callable(provider):
        return _INVALID_CONFIGURATION
    try:
        contract = provider()
        if not _valid_agent_runtime_contract(contract, runtime_class, schema):
            return _INVALID_CONFIGURATION
        assert isinstance(contract, Mapping)
        settings = contract["settings"]
        assert isinstance(settings, Mapping)
        return {
            "schema_version": schema.schema_version,
            "schema_id": schema.schema_id,
            "implementation": runtime_class,
            "runtime_backend": schema.runtime_backend,
            "runtime_handle_backend": schema.runtime_handle_backend,
            "settings": dict(settings),
        }
    except Exception:  # noqa: BLE001 - opaque runtime authority fails closed
        return _INVALID_CONFIGURATION


def _valid_agent_runtime_contract(
    contract: object,
    runtime_class: str,
    schema: _AgentRuntimeSchema,
) -> bool:
    if not isinstance(contract, Mapping):
        return False
    if not _json_primitive_tree(contract, reject_sensitive_keys=True):
        return False
    if set(contract) != _AGENT_RUNTIME_CONTRACT_KEYS:
        return False
    if (
        type(contract.get("schema_version")) is not int
        or contract.get("schema_version") != schema.schema_version
        or contract.get("schema_id") != schema.schema_id
        or contract.get("implementation") != runtime_class
        or contract.get("runtime_backend") != schema.runtime_backend
        or contract.get("runtime_handle_backend") != schema.runtime_handle_backend
    ):
        return False
    settings = contract.get("settings")
    if not isinstance(settings, Mapping) or set(settings) != set(schema.setting_kinds):
        return False
    for name, kind in schema.setting_kinds.items():
        if not _valid_agent_runtime_setting(settings.get(name), kind):
            return False
    for name, allowed in schema.literal_settings.items():
        if settings.get(name) not in allowed:
            return False
    if schema.schema_id == "ouroboros.codex_cli_runtime.v4":
        entry_count = settings.get("child_environment_entry_count")
        sensitive_entry_count = settings.get("child_environment_sensitive_entry_count")
        if (
            type(entry_count) is not int
            or type(sensitive_entry_count) is not int
            or sensitive_entry_count > entry_count
        ):
            return False
        if settings.get("cli_path") != settings.get("cli_executable_path"):
            return False
        if settings.get("skills_source") == "package" and not str(
            settings.get("skills_location", "")
        ).startswith("packaged://"):
            return False
        if (
            settings.get("skill_dispatcher_kind") == "packaged"
            and settings.get("skill_dispatcher_identity") != "packaged"
        ):
            return False
    return True


def _valid_agent_runtime_setting(value: object, kind: str) -> bool:
    if kind == "optional_text":
        return value is None or _configuration_text(value) is not _INVALID_CONFIGURATION
    if kind == "text":
        return _configuration_text(value) is not _INVALID_CONFIGURATION
    if kind == "sha256":
        return _valid_sha256(value)
    if kind == "absolute_path":
        return (
            _configuration_text(value) is not _INVALID_CONFIGURATION
            and isinstance(value, str)
            and Path(value).is_absolute()
        )
    if kind == "skill_location":
        return (
            _configuration_text(value) is not _INVALID_CONFIGURATION
            and isinstance(value, str)
            and (value.startswith("packaged://") or Path(value).is_absolute())
        )
    if kind == "dispatcher_identity":
        return value == "packaged" or _valid_sha256(value)
    if kind == "positive_number":
        return _positive_finite(value) is not None
    if kind == "optional_positive_number":
        return value is None or _positive_finite(value) is not None
    if kind == "positive_int":
        return type(value) is int and value > 0
    if kind == "non_negative_int":
        return type(value) is int and value >= 0
    if kind == "bool":
        return isinstance(value, bool)
    if kind == "string_sequence":
        return (
            isinstance(value, list | tuple)
            and all(_configuration_text(item) is not _INVALID_CONFIGURATION for item in value)
            and len(set(value)) == len(value)
            and list(value) == sorted(value)
        )
    if kind == "relative_posix_path":
        if (
            _configuration_text(value) is _INVALID_CONFIGURATION
            or not isinstance(value, str)
            or "\\" in value
        ):
            return False
        parsed = PurePosixPath(value)
        return not parsed.is_absolute() and ".." not in parsed.parts and str(parsed) == value
    return False


def _valid_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _agent_runtime_configuration(
    runtime: Any,
    kwargs: Mapping[str, object],
    *,
    tools: list[str] | None,
    system_prompt: str | None,
) -> dict[str, object]:
    runtime_backend = _configuration_text(
        _runtime_configuration_setting(runtime, "runtime_backend", "_runtime_backend"),
        fallback=f"{type(runtime).__module__}.{type(runtime).__qualname__}",
    )
    known_kwargs = {
        "model",
        "permission_mode",
        "reasoning_effort",
        "resume_handle",
        "resume_session_id",
        "service_tier",
    }
    extra_request_settings = {
        key: value for key, value in kwargs.items() if key not in known_kwargs
    }
    return {
        "surface": "agent_runtime",
        "runtime_attestation": _agent_runtime_attestation_configuration(runtime),
        "runtime_backend": runtime_backend,
        "llm_backend": _configuration_text(
            _runtime_configuration_setting(runtime, "llm_backend", "_llm_backend"),
            fallback=runtime_backend,
        ),
        "model": _configuration_text(
            kwargs.get("model"),
            fallback=_runtime_configuration_setting(runtime, "model", "_model"),
        ),
        "reasoning_effort": _configuration_text(
            kwargs.get("reasoning_effort"),
            fallback=_runtime_configuration_setting(
                runtime,
                "reasoning_effort",
                "_reasoning_effort",
            ),
            optional=True,
        ),
        "service_tier": _configuration_text(
            kwargs.get("service_tier"),
            fallback=_runtime_configuration_setting(runtime, "service_tier", "_service_tier"),
            optional=True,
        ),
        "permission_mode": _configuration_text(
            kwargs.get("permission_mode"),
            fallback=_runtime_configuration_setting(
                runtime,
                "permission_mode",
                "_permission_mode",
            ),
            optional=True,
        ),
        "resume_handle": _resume_handle_configuration(kwargs.get("resume_handle")),
        "resume_session": _resume_session_configuration(kwargs.get("resume_session_id")),
        "tools": _tools_configuration(tools),
        "system_prompt_digest": _optional_text_digest(system_prompt),
        "extra_request_settings": extra_request_settings,
    }


def _configuration_text(
    value: object,
    *,
    fallback: object = None,
    optional: bool = False,
) -> object:
    candidate = value if value is not None else fallback
    if candidate is None:
        return None if optional else _INVALID_CONFIGURATION
    if not isinstance(candidate, str) or not candidate.strip():
        return _INVALID_CONFIGURATION
    normalized = candidate.strip()
    return _INVALID_CONFIGURATION if normalized.lower() == "unknown" else normalized


def _resume_handle_configuration(handle: object) -> object:
    if handle is None:
        return {"mode": "fresh"}
    backend = _configuration_text(_runtime_setting(handle, "backend"))
    kind = _configuration_text(_runtime_setting(handle, "kind"))
    metadata = getattr(handle, "metadata", None)
    if (
        backend is _INVALID_CONFIGURATION
        or kind is _INVALID_CONFIGURATION
        or not isinstance(metadata, Mapping)
    ):
        return _INVALID_CONFIGURATION
    resume_identifiers = (
        getattr(handle, "native_session_id", None),
        getattr(handle, "conversation_id", None),
        getattr(handle, "previous_response_id", None),
        getattr(handle, "transcript_path", None),
        metadata.get("server_session_id"),
    )
    if any(value is not None for value in resume_identifiers):
        # The runtime would inherit arm-specific transcript state. Without a
        # stable semantic context fingerprint it is not a comparable provider call.
        return _INVALID_CONFIGURATION
    if set(metadata) - _SCOPED_HANDLE_METADATA_FIELDS:
        return _INVALID_CONFIGURATION
    semantic_metadata = {
        key: metadata[key] for key in ("level_number", "scope", "session_role") if key in metadata
    }
    if not _json_primitive_tree(semantic_metadata):
        return _INVALID_CONFIGURATION
    return {
        "mode": "scoped_fresh",
        "backend": backend,
        "kind": kind,
        "approval_mode": _configuration_text(
            _runtime_configuration_setting(handle, "approval_mode"),
            fallback="none",
        ),
        "cwd_bound": bool(_runtime_setting(handle, "cwd")),
        "semantic_metadata": semantic_metadata,
    }


def _resume_session_configuration(session_id: object) -> object:
    if session_id is None:
        return "fresh"
    if isinstance(session_id, str) and session_id.strip():
        return _INVALID_CONFIGURATION
    return _INVALID_CONFIGURATION


def _tools_configuration(tools: list[str] | None) -> object:
    if tools is None:
        return None
    if not isinstance(tools, list) or any(not isinstance(tool, str) or not tool for tool in tools):
        return _INVALID_CONFIGURATION
    return tuple(tools)


def _optional_text_digest(value: str | None) -> object:
    if value is None:
        return None
    if not isinstance(value, str):
        return _INVALID_CONFIGURATION
    return "sha256:" + hashlib.sha256(value.encode()).hexdigest()


def _json_primitive_tree(
    value: object,
    *,
    reject_sensitive_keys: bool = False,
) -> bool:
    """Validate a bounded JSON tree without recursive traversal."""
    stack: list[tuple[object, int, bool]] = [(value, 0, False)]
    active_containers: set[int] = set()
    node_count = 0
    while stack:
        current, depth, exiting = stack.pop()
        if exiting:
            active_containers.discard(id(current))
            continue
        node_count += 1
        if node_count > _MAX_CONFIGURATION_NODES:
            return False
        if current is None or isinstance(current, int | bool):
            continue
        if isinstance(current, float):
            if not math.isfinite(current):
                return False
            continue
        if isinstance(current, str):
            if len(current) > _MAX_CONFIGURATION_TEXT_LENGTH:
                return False
            continue
        if depth >= _MAX_CONFIGURATION_DEPTH:
            return False
        if isinstance(current, Mapping):
            identity = id(current)
            if identity in active_containers:
                return False
            active_containers.add(identity)
            try:
                if len(current) > _MAX_CONFIGURATION_NODES:
                    return False
                items = tuple(current.items())
            except Exception:  # noqa: BLE001 — opaque mappings fail closed
                return False
            for key, _item in items:
                if not isinstance(key, str) or len(key) > _MAX_CONFIGURATION_TEXT_LENGTH:
                    return False
                if reject_sensitive_keys and _sensitive_configuration_key(key):
                    return False
            stack.append((current, depth, True))
            stack.extend((item, depth + 1, False) for _, item in reversed(items))
            continue
        if isinstance(current, list | tuple):
            identity = id(current)
            if identity in active_containers:
                return False
            active_containers.add(identity)
            if len(current) > _MAX_CONFIGURATION_NODES:
                return False
            stack.append((current, depth, True))
            stack.extend((item, depth + 1, False) for item in reversed(current))
            continue
        return False
    return True


def _sensitive_configuration_key(key: str) -> bool:
    normalized = "_".join(
        part
        for part in "".join(
            character.lower() if character.isalnum() else " " for character in key
        ).split()
    )
    return normalized in _SENSITIVE_CONFIGURATION_KEYS


def _canonical_configuration(configuration: Mapping[str, object]) -> str:
    if not _json_primitive_tree(configuration):
        raise ValueError("provider configuration is not a finite JSON primitive tree")
    return json.dumps(
        dict(configuration),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    )


def _fingerprint_configurations(configurations: Iterator[str]) -> str:
    encoded = json.dumps(
        sorted(set(configurations)),
        separators=(",", ":"),
        ensure_ascii=True,
    )
    return "sha256:" + hashlib.sha256(encoded.encode()).hexdigest()


def _configuration_assignments(
    calls: list[_ProviderCall],
) -> tuple[tuple[str, tuple[str, ...]], ...]:
    """Bind each stable provider role to its ordered configuration calls."""
    by_role: dict[str, list[str]] = {}
    for call in calls:
        by_role.setdefault(call.role, []).append(
            _fingerprint_configurations(iter((call.configuration,)))
        )
    return tuple((role, tuple(configurations)) for role, configurations in sorted(by_role.items()))


def _positive_finite(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    try:
        number = float(value)
    except (OverflowError, ValueError):
        return None
    return number if math.isfinite(number) and number > 0 else None


def _finite_non_negative(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    try:
        number = float(value)
    except (OverflowError, ValueError):
        return None
    return number if math.isfinite(number) and number >= 0 else None


def _non_negative_counter(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


_FALLBACK_USAGE_KEYS = (
    "input_tokens",
    "output_tokens",
    "cache_creation_input_tokens",
    "cache_read_input_tokens",
)
_USAGE_KEYS = (*_FALLBACK_USAGE_KEYS, "cached_input_tokens", "total_tokens")


def _message_token_spend(messages: Any) -> float | None:
    """Use the same all-or-nothing semantics as executor token attribution."""
    spend = 0.0
    observed = False
    try:
        iterator: AsyncIterator[Any] | Iterator[Any] = iter(messages)
    except TypeError:
        return None
    for message in iterator:
        data = getattr(message, "data", None)
        if not isinstance(data, Mapping):
            continue
        if data.get("usage_invalid") is True:
            return None
        if "usage" not in data:
            continue
        usage = data["usage"]
        if not isinstance(usage, Mapping):
            return None
        unknown_token_keys = {
            key
            for key in usage
            if isinstance(key, str) and key.endswith("_tokens") and key not in _USAGE_KEYS
        }
        if unknown_token_keys:
            return None
        normalized: dict[str, int] = {}
        for key in _USAGE_KEYS:
            if key not in usage:
                continue
            raw = usage[key]
            counter = _non_negative_counter(raw)
            if counter is None or _finite_non_negative(counter) is None:
                return None
            normalized[key] = counter
        if "total_tokens" in normalized:
            fallback_keys_present = {key for key in _FALLBACK_USAGE_KEYS if key in normalized}
            if fallback_keys_present and not {"input_tokens", "output_tokens"}.issubset(
                fallback_keys_present
            ):
                return None
            components = [normalized[key] for key in _FALLBACK_USAGE_KEYS if key in normalized]
            if components and normalized["total_tokens"] != sum(components):
                return None
            spend += float(normalized["total_tokens"])
            observed = True
            continue
        components = [normalized[key] for key in _FALLBACK_USAGE_KEYS if key in normalized]
        if {"input_tokens", "output_tokens"}.issubset(normalized):
            component_total = sum(components)
            if _finite_non_negative(component_total) is None:
                return None
            spend += float(component_total)
            observed = True
        elif components:
            return None
    return spend if observed and spend > 0 and math.isfinite(spend) else None


def _bind_stream_effective_model(
    capture: GenerationProviderUsageCapture,
    call: _ProviderCall,
    messages: Any,
) -> None:
    observations_present = False
    malformed = False
    effective_models: list[str] = []
    try:
        iterator: Iterator[Any] = iter(messages)
    except TypeError:
        capture.mark_incomplete()
        return
    for message in iterator:
        data = getattr(message, "data", None)
        if not isinstance(data, Mapping) or "model_observation" not in data:
            continue
        observations_present = True
        observation = data["model_observation"]
        if not isinstance(observation, Mapping) or "effective_model" not in observation:
            malformed = True
            continue
        raw_model = observation["effective_model"]
        if raw_model is None:
            continue
        if not isinstance(raw_model, str) or not raw_model.strip():
            malformed = True
            continue
        effective_models.append(raw_model.strip())
    if not observations_present or malformed or not effective_models:
        capture.mark_incomplete()
        return
    if len(set(effective_models)) > 1:
        capture.mark_incomplete()
        return
    if effective_models:
        capture.resolve_model(call, effective_models[0])


__all__ = [
    "GenerationProviderUsageCapture",
    "ProviderUsageSummary",
    "call",
    "capture_generation_provider_usage",
    "capture_provider_usage",
    "current_summary",
    "observe_callable",
    "tracked_agent_task",
    "tracked_agent_task_to_result",
    "tracked_complete",
]
