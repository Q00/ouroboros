"""LiteLLM adapter for unified LLM provider access.

This module provides the LiteLLMAdapter class that implements the LLMAdapter
protocol using LiteLLM for multi-provider support including OpenRouter.
"""

import asyncio
from dataclasses import dataclass, field, replace
import hashlib
import importlib.metadata
import importlib.util
import json
import math
import os
from pathlib import Path
import sys
import tempfile
import threading
import types
from typing import TYPE_CHECKING, Any

import httpx
import litellm
from litellm.llms.custom_httpx.http_handler import AsyncHTTPHandler
import openai
import structlog

from ouroboros.core.errors import ProviderError
from ouroboros.core.retry import retry_async
from ouroboros.core.security import MAX_LLM_RESPONSE_LENGTH, InputValidator
from ouroboros.core.types import Result
from ouroboros.events.io_recorder import get_current_io_journal_recorder
from ouroboros.providers.base import (
    CompletionConfig,
    CompletionResponse,
    Message,
    UsageInfo,
)
from ouroboros.providers.frugality_attestation import (
    PreparedFrugalityCompletion,
    credential_authority_digest,
    effective_completion_request,
)
from ouroboros.providers.profiles import resolve_completion_profile_result

_MINIMAX_MODEL_COST_OVERRIDES = {
    "minimax/MiniMax-M2.7": {
        "cache_creation_input_token_cost": 3.75e-7,
        "cache_read_input_token_cost": 6e-8,
        "input_cost_per_token": 3e-7,
        "litellm_provider": "minimax",
        "max_input_tokens": 204800,
        "mode": "chat",
        "output_cost_per_token": 1.2e-6,
        "supports_function_calling": True,
        "supports_prompt_caching": True,
        "supports_reasoning": True,
        "supports_system_messages": True,
        "supports_tool_choice": True,
    }
}

# Keep reviewed model metadata available while the optional dependency remains
# exact-pinned for reproducible installs.
litellm.register_model(_MINIMAX_MODEL_COST_OVERRIDES)

if TYPE_CHECKING:
    from ouroboros.events.io_recorder import IOJournalRecorder

log = structlog.get_logger()
_CREDENTIALS_UNSET = object()
_PLACEHOLDER_API_KEY_PREFIX = "YOUR_"
_PLACEHOLDER_API_KEY_SUFFIX = "_API_KEY"
_PROOF_DEFAULT_API_BASES = {
    "anthropic": "https://api.anthropic.com",
    "google": "https://generativelanguage.googleapis.com",
    "openai": "https://api.openai.com/v1",
    "openrouter": "https://openrouter.ai/api/v1",
}
_PROOF_API_BASE_ENVIRONMENT = {
    "anthropic": ("ANTHROPIC_API_BASE", "ANTHROPIC_BASE_URL"),
    "google": ("GEMINI_API_BASE",),
    "openai": ("OPENAI_API_BASE", "OPENAI_BASE_URL"),
    "openrouter": ("OPENROUTER_API_BASE",),
}
_PROOF_NONE_GLOBALS = (
    "aclient_session",
    "add_user_information_to_llm_headers",
    "api_version",
    "client_session",
    "headers",
    "organization",
    "project",
    "proxy_auth",
    "ssl_certificate",
    "ssl_ecdh_curve",
    "ssl_security_level",
)
_PROOF_FALSE_GLOBALS = (
    "add_function_to_prompt",
    "aiohttp_trust_env",
    "check_provider_endpoint",
    "disable_add_transform_inline_image_block",
    "disable_aiohttp_transport",
    "disable_aiohttp_trust_env",
    "drop_params",
    "enable_json_schema_validation",
    "enable_preview_features",
    "filter_invalid_headers",
    "force_ipv4",
    "modify_params",
    "network_mock",
    "route_all_chat_openai_to_responses",
    "strip_anthropic_total_tokens",
    "use_chat_completions_url_for_anthropic_messages",
)
_PROOF_TRUE_GLOBALS = (
    "ssl_verify",
    "use_aiohttp_transport",
)
_PROOF_EMPTY_SEQUENCE_GLOBALS = (
    "callbacks",
    "failure_callback",
    "input_callback",
    "post_call_rules",
    "pre_call_rules",
    "service_callback",
    "success_callback",
)
_PROOF_EMPTY_MAPPING_GLOBALS = (
    "custom_prompt_dict",
    "guardrail_name_config_map",
    "model_alias_map",
)
_PROOF_UNSEALED_PROVIDER_ENVIRONMENT = {
    "anthropic": ("ANTHROPIC_API_VERSION", "LITELLM_ANTHROPIC_DISABLE_URL_SUFFIX"),
    "google": (
        "GEMINI_API_VERSION",
        "GOOGLE_CLOUD_LOCATION",
        "GOOGLE_CLOUD_PROJECT",
        "GOOGLE_GENAI_USE_VERTEXAI",
    ),
    "openai": ("OPENAI_API_VERSION", "OPENAI_ORGANIZATION", "OPENAI_PROJECT"),
    "openrouter": ("OR_APP_NAME", "OR_SITE_URL"),
}
_PROOF_UNSEALED_TRANSPORT_ENVIRONMENT = (
    "AIOHTTP_TRUST_ENV",
    "ALL_PROXY",
    "CURL_CA_BUNDLE",
    "DISABLE_AIOHTTP_TRANSPORT",
    "DISABLE_AIOHTTP_TRUST_ENV",
    "EXPERIMENTAL_OPENAI_BASE_LLM_HTTP_HANDLER",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "LITELLM_USER_AGENT",
    "NO_PROXY",
    "REQUESTS_CA_BUNDLE",
    "SSL_CERTIFICATE",
    "SSL_CERT_DIR",
    "SSL_CERT_FILE",
    "SSL_ECDH_CURVE",
    "SSL_SECURITY_LEVEL",
    "SSL_VERIFY",
    "all_proxy",
    "http_proxy",
    "https_proxy",
    "no_proxy",
)
_PROOF_PROVIDER_CONFIG_DEFAULTS = {
    "anthropic": ("AnthropicConfig", {"max_tokens": 4096}),
    "google": ("GoogleAIStudioGeminiConfig", {}),
    "openai": ("OpenAIConfig", {}),
    "openrouter": ("OpenrouterConfig", {}),
}
_PROOF_LITELLM_MAX_RETRIES = 0
_PROOF_TRANSPORT_CONTRACT = "ouroboros.httpx.async.v1"
_PROOF_ISOLATION_KIND = "subprocess"
_PROOF_WORKER_PROTOCOL = "ouroboros.litellm.worker.v1"
_PROOF_WORKER_ENVIRONMENT_CONTRACT = "ouroboros.empty-env.v1"
_PROOF_WORKER_CWD_CONTRACT = "ouroboros.private-empty-tempdir.v1"
_PROOF_MUTATION_AUDIT = "litellm.module-setattr.v1"
_PROOF_WORKER_MAX_OUTPUT_BYTES = 2 * 1024 * 1024
_PROOF_OPENAI_CLIENT_TYPE = "openai.AsyncOpenAI"
_PROOF_HTTP_HANDLER_CLIENT_TYPE = "litellm.AsyncHTTPHandler"
try:
    _LITELLM_DEPENDENCY_VERSION = importlib.metadata.version("litellm")
except importlib.metadata.PackageNotFoundError:
    _LITELLM_DEPENDENCY_VERSION = None
try:
    _HTTPX_DEPENDENCY_VERSION = importlib.metadata.version("httpx")
except importlib.metadata.PackageNotFoundError:
    _HTTPX_DEPENDENCY_VERSION = None
try:
    _OPENAI_DEPENDENCY_VERSION = importlib.metadata.version("openai")
except importlib.metadata.PackageNotFoundError:
    _OPENAI_DEPENDENCY_VERSION = None

# LiteLLM exceptions that should trigger retries
RETRIABLE_EXCEPTIONS = (
    litellm.RateLimitError,
    litellm.ServiceUnavailableError,
    litellm.Timeout,
    litellm.APIConnectionError,
)


@dataclass(frozen=True, slots=True)
class _LiteLLMDependencyAuthority:
    """Non-secret proof eligibility for LiteLLM's process-global state."""

    version: str | None
    provider: str
    defaults_verified: bool


@dataclass(frozen=True, slots=True)
class _LiteLLMWorkerAuthority:
    """Exact interpreter and worker implementation used for provider effects."""

    launcher_path: str | None
    resolved_executable_path: str | None
    executable_content_sha256: str | None
    python_version: str | None
    worker_module: str
    worker_implementation_sha256: str | None
    adapter_module: str
    adapter_implementation_sha256: str | None
    ouroboros_implementation_contract: str
    ouroboros_implementation_sha256: str | None


@dataclass(slots=True)
class _LiteLLMDispatchState:
    """Ephemeral proof status that must never enter durable receipts."""

    dispatch_started: bool = False
    cleanup_complete: bool = False
    cleanup_failed: bool = False
    authority_drifted: bool = False


@dataclass(slots=True)
class _SealedLiteLLMClient:
    """A dedicated client whose transport is independent of process globals."""

    value: openai.AsyncOpenAI | AsyncHTTPHandler = field(repr=False)
    client_type: str

    async def close(self) -> None:
        await self.value.close()


class _SealedAsyncHTTPHandler(AsyncHTTPHandler):
    """LiteLLM-compatible handler backed by an explicitly constructed client."""

    def __init__(self, client: httpx.AsyncClient, *, timeout: float | None) -> None:
        # Do not call AsyncHTTPHandler.__init__: it resolves mutable LiteLLM and
        # environment transport authority while constructing its client.
        self.timeout = timeout
        self.event_hooks = None
        self.client = client
        self.client_alias = "ouroboros-frugality-proof"


@dataclass(frozen=True, slots=True)
class _PreparedLiteLLMDispatch:
    """In-memory route authority; secrets never enter the proof contract."""

    config: CompletionConfig
    api_key: str | None = field(repr=False)
    api_base: str | None = field(repr=False)
    timeout: float | None
    max_retries: int
    dependency_authority: _LiteLLMDependencyAuthority
    worker_authority: _LiteLLMWorkerAuthority
    client_type: str | None
    state: _LiteLLMDispatchState = field(
        default_factory=_LiteLLMDispatchState,
        repr=False,
        compare=False,
    )


_PROOF_AUDITED_GLOBALS = frozenset(
    {
        *_PROOF_NONE_GLOBALS,
        *_PROOF_FALSE_GLOBALS,
        *_PROOF_TRUE_GLOBALS,
        *_PROOF_EMPTY_SEQUENCE_GLOBALS,
        *_PROOF_EMPTY_MAPPING_GLOBALS,
        "api_base",
        "api_key",
        "anthropic_key",
        "gemini_key",
        "openai_key",
        "openrouter_key",
    }
)
_PROOF_AUDIT_LOCK = threading.Lock()
_ACTIVE_PROOF_AUDITS: dict[int, _LiteLLMDispatchState] = {}


class _AuditedLiteLLMModule(types.ModuleType):
    """Record every protected LiteLLM assignment while proof work is active."""

    def __setattr__(self, name: str, value: object) -> None:
        if name in _PROOF_AUDITED_GLOBALS:
            with _PROOF_AUDIT_LOCK:
                states = tuple(_ACTIVE_PROOF_AUDITS.values())
            for state in states:
                state.authority_drifted = True
        super().__setattr__(name, value)


def _register_proof_authority_audit(state: _LiteLLMDispatchState) -> None:
    """Install an event-driven parent mutation audit for one isolated dispatch."""
    try:
        if not isinstance(litellm, _AuditedLiteLLMModule):
            litellm.__class__ = _AuditedLiteLLMModule
    except (AttributeError, TypeError):
        state.authority_drifted = True
    with _PROOF_AUDIT_LOCK:
        _ACTIVE_PROOF_AUDITS[id(state)] = state


def _unregister_proof_authority_audit(state: _LiteLLMDispatchState) -> None:
    with _PROOF_AUDIT_LOCK:
        _ACTIVE_PROOF_AUDITS.pop(id(state), None)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _module_implementation_sha256(module_name: str) -> str:
    module_spec = importlib.util.find_spec(module_name)
    module_origin = getattr(module_spec, "origin", None)
    if not isinstance(module_origin, str):
        raise ValueError(f"{module_name} implementation is unavailable")
    module_path = Path(module_origin).resolve(strict=True)
    if not module_path.is_file():
        raise ValueError(f"{module_name} implementation must be a regular file")
    return _sha256_file(module_path)


def _ouroboros_implementation_sha256() -> str:
    """Bind every Python source that can enter the isolated worker import graph."""
    package_spec = importlib.util.find_spec("ouroboros")
    locations = getattr(package_spec, "submodule_search_locations", None)
    if locations is None:
        raise ValueError("ouroboros package implementation is unavailable")
    package_roots = tuple(locations)
    if len(package_roots) != 1:
        raise ValueError("ouroboros package implementation must have one source root")
    package_root = Path(package_roots[0]).resolve(strict=True)
    if not package_root.is_dir():
        raise ValueError("ouroboros package source root must be a directory")

    source_files = sorted(package_root.rglob("*.py"))
    if not source_files:
        raise ValueError("ouroboros package contains no Python sources")
    digest = hashlib.sha256()
    for source_file in source_files:
        if source_file.is_symlink():
            raise ValueError("ouroboros package sources must not be symlinks")
        resolved_source = source_file.resolve(strict=True)
        resolved_source.relative_to(package_root)
        if not resolved_source.is_file():
            raise ValueError("ouroboros package source must be a regular file")
        relative_path = resolved_source.relative_to(package_root).as_posix().encode("utf-8")
        content = resolved_source.read_bytes()
        digest.update(len(relative_path).to_bytes(8, "big"))
        digest.update(relative_path)
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return digest.hexdigest()


def _worker_runtime_authority(launcher: object) -> _LiteLLMWorkerAuthority:
    worker_module = "ouroboros.providers.litellm_proof_worker"
    adapter_module = "ouroboros.providers.litellm_adapter"
    implementation_contract = "ouroboros.python-sources.v1"
    try:
        if not isinstance(launcher, str) or not launcher or not Path(launcher).is_absolute():
            raise ValueError("worker launcher must be absolute")
        launcher_path = Path(launcher)
        resolved_executable = launcher_path.resolve(strict=True)
        if not resolved_executable.is_file() or not os.access(launcher_path, os.X_OK):
            raise ValueError("worker launcher must resolve to an executable file")
        return _LiteLLMWorkerAuthority(
            launcher_path=str(launcher_path),
            resolved_executable_path=str(resolved_executable),
            executable_content_sha256=_sha256_file(resolved_executable),
            python_version=sys.version.split()[0],
            worker_module=worker_module,
            worker_implementation_sha256=_module_implementation_sha256(worker_module),
            adapter_module=adapter_module,
            adapter_implementation_sha256=_module_implementation_sha256(adapter_module),
            ouroboros_implementation_contract=implementation_contract,
            ouroboros_implementation_sha256=_ouroboros_implementation_sha256(),
        )
    except (OSError, TypeError, ValueError):
        return _LiteLLMWorkerAuthority(
            launcher_path=None,
            resolved_executable_path=None,
            executable_content_sha256=None,
            python_version=None,
            worker_module=worker_module,
            worker_implementation_sha256=None,
            adapter_module=adapter_module,
            adapter_implementation_sha256=None,
            ouroboros_implementation_contract=implementation_contract,
            ouroboros_implementation_sha256=None,
        )


def _serialise_messages_for_hash(
    messages: list[Message],
    request_options: dict[str, Any] | None = None,
) -> str:
    """Build a deterministic string of the request payload for hashing.

    Used by the I/O Journal recorder (#517) to compute ``prompt_hash``.
    The string is stable for identical input and request-shaping options
    so materially different LiteLLM calls do not collapse to the same
    hash; it is not the wire payload.
    """
    payload: dict[str, Any] = {
        "messages": [{"role": str(m.role), "content": m.content} for m in messages]
    }
    if request_options:
        payload["request_options"] = {
            key: value for key, value in request_options.items() if value is not None
        }
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def _record_litellm_completion(call: Any, parsed: CompletionResponse) -> None:
    """Populate the recorder's LLMCallRecord from the parsed response.

    Recording the same ``CompletionResponse`` returned to callers keeps
    the journal aligned with adapter-visible truncation and normalization.
    """
    call.record_completion(
        completion_text=parsed.content,
        finish_reason=parsed.finish_reason,
        token_count_in=parsed.usage.prompt_tokens if parsed.usage else None,
        token_count_out=parsed.usage.completion_tokens if parsed.usage else None,
    )


def _request_options_for_hash(config: CompletionConfig) -> dict[str, Any]:
    """Return request-shaping options not already first-class journal fields."""
    options: dict[str, Any] = {}
    model_lower = config.model.lower()
    if not ("anthropic" in model_lower or "claude" in model_lower):
        options["top_p"] = config.top_p
    if config.stop:
        options["stop"] = config.stop
    if config.response_format:
        options["response_format"] = config.response_format
    if config.reasoning_effort:
        options["reasoning_effort"] = config.reasoning_effort
    return options


class LiteLLMAdapter:
    """LLM adapter using LiteLLM for unified provider access.

    This adapter supports multiple LLM providers through LiteLLM's unified
    interface, including OpenRouter for model routing.

    API keys are loaded from environment variables with the following priority:
    1. Environment variables: OPENROUTER_API_KEY, OPENAI_API_KEY, ANTHROPIC_API_KEY
    2. Explicit api_key parameter (overrides environment)

    Example:
        # Using environment variables (recommended)
        adapter = LiteLLMAdapter()

        # Or with explicit API key
        adapter = LiteLLMAdapter(api_key="sk-...")

        result = await adapter.complete(
            messages=[Message(role=MessageRole.USER, content="Hello!")],
            config=CompletionConfig(model="openrouter/openai/gpt-4"),
        )
    """

    def __init__(
        self,
        *,
        api_key: str | None = None,
        api_base: str | None = None,
        timeout: float | None = 60.0,
        max_retries: int = 3,
        io_recorder: "IOJournalRecorder | None" = None,
        frugality_proof: bool = False,
    ) -> None:
        """Initialize the LiteLLM adapter.

        Args:
            api_key: Optional API key (overrides environment variables).
            api_base: Optional API base URL for custom endpoints.
            timeout: Request timeout in seconds. Default 60.0.
            max_retries: Maximum number of retries for transient errors. Default 3.
            io_recorder: Optional :class:`IOJournalRecorder` (M3 / #517).
                When provided, every retry attempt of the outbound LLM
                call is wrapped in the recorder so paired
                ``llm.call.requested`` / ``llm.call.returned`` events
                land on the EventStore — each retry produces its own
                pair, so failures across retries appear in the journal
                as distinct attempts. Default ``None`` is byte-for-byte
                the previous behaviour.
            frugality_proof: Seal one measurable attempt, a bounded timeout,
                and the provider route used by focused-evolution proof.
        """
        self._api_key = api_key
        self._api_base = api_base
        self._timeout = 60.0 if frugality_proof and timeout is None else timeout
        self._max_retries = 1 if frugality_proof else max_retries
        self._credentials_cache: object = _CREDENTIALS_UNSET
        self._io_recorder = io_recorder
        self._frugality_proof = frugality_proof

    def frugality_prepare_completion(
        self,
        config: CompletionConfig,
    ) -> Result[PreparedFrugalityCompletion, ProviderError]:
        """Resolve once, seal dispatch, and attest the non-secret execution envelope."""
        profile_result = resolve_completion_profile_result(config, backend="litellm")
        if profile_result.is_err:
            return Result.err(profile_result.error)
        resolved = profile_result.value
        effective_config = resolved.config
        api_key = self._get_api_key(effective_config.model)
        api_base = self._get_api_base(effective_config.model)
        dependency_authority = self._dependency_authority(
            effective_config.model,
            api_key=api_key,
        )
        worker_authority = _worker_runtime_authority(sys.executable)
        client_type = self._proof_client_type(dependency_authority.provider)
        contract: dict[str, object] = {
            "schema_version": 7,
            "schema_id": "ouroboros.litellm.v7",
            "internal_attempt_limit": self._max_retries,
            "credential_authority": credential_authority_digest(api_key),
            "effective_request": effective_completion_request(
                resolved,
                provider_model=effective_config.model,
            ),
            "settings": {
                "api_base": api_base,
                "timeout": self._timeout,
                "max_retries": self._max_retries,
                "io_recorder_configured": self._io_recorder is not None,
                "dependency_version": dependency_authority.version,
                "dependency_provider": dependency_authority.provider,
                "dependency_defaults_verified": dependency_authority.defaults_verified,
                "dependency_max_retries": _PROOF_LITELLM_MAX_RETRIES,
                "dependency_client_sealed": client_type is not None,
                "dependency_client_type": client_type,
                "dependency_transport_contract": _PROOF_TRANSPORT_CONTRACT,
                "dependency_cleanup_required": True,
                "dependency_isolation_kind": _PROOF_ISOLATION_KIND,
                "dependency_worker_protocol": _PROOF_WORKER_PROTOCOL,
                "dependency_worker_environment_contract": (_PROOF_WORKER_ENVIRONMENT_CONTRACT),
                "dependency_worker_cwd_contract": _PROOF_WORKER_CWD_CONTRACT,
                "dependency_mutation_audit": _PROOF_MUTATION_AUDIT,
                "worker_launcher_path": worker_authority.launcher_path,
                "worker_executable_resolved_path": (worker_authority.resolved_executable_path),
                "worker_executable_content_sha256": (worker_authority.executable_content_sha256),
                "worker_python_version": worker_authority.python_version,
                "worker_module": worker_authority.worker_module,
                "worker_implementation_sha256": (worker_authority.worker_implementation_sha256),
                "adapter_module": worker_authority.adapter_module,
                "adapter_implementation_sha256": (worker_authority.adapter_implementation_sha256),
                "ouroboros_implementation_contract": (
                    worker_authority.ouroboros_implementation_contract
                ),
                "ouroboros_implementation_sha256": (
                    worker_authority.ouroboros_implementation_sha256
                ),
                "httpx_dependency_version": _HTTPX_DEPENDENCY_VERSION,
                "openai_dependency_version": _OPENAI_DEPENDENCY_VERSION,
            },
        }
        return Result.ok(
            PreparedFrugalityCompletion(
                config=replace(effective_config, role=None, profile=None),
                contract=contract,
                dispatch=_PreparedLiteLLMDispatch(
                    config=replace(effective_config, role=None, profile=None),
                    api_key=api_key,
                    api_base=api_base,
                    timeout=self._timeout,
                    max_retries=self._max_retries,
                    dependency_authority=dependency_authority,
                    worker_authority=worker_authority,
                    client_type=client_type,
                ),
            )
        )

    def frugality_validate_prepared_completion(
        self,
        prepared: PreparedFrugalityCompletion,
    ) -> bool:
        """Revalidate mutable dependency authority before and after dispatch."""
        dispatch = prepared.dispatch
        if not isinstance(dispatch, _PreparedLiteLLMDispatch) or dispatch.config != prepared.config:
            return False
        current_authority = self._dependency_authority(
            prepared.config.model, api_key=dispatch.api_key
        )
        current_worker_authority = _worker_runtime_authority(
            dispatch.worker_authority.launcher_path
        )
        return bool(
            dispatch.client_type is not None
            and not dispatch.state.authority_drifted
            and not dispatch.state.cleanup_failed
            and (not dispatch.state.dispatch_started or dispatch.state.cleanup_complete)
            and dispatch.dependency_authority == current_authority
            and dispatch.worker_authority == current_worker_authority
        )

    async def frugality_complete_prepared(
        self,
        messages: list[Message],
        prepared: PreparedFrugalityCompletion,
    ) -> Result[CompletionResponse, ProviderError]:
        """Dispatch with the exact route authority attested during preparation."""
        dispatch = prepared.dispatch
        if not isinstance(dispatch, _PreparedLiteLLMDispatch) or dispatch.config != prepared.config:
            return Result.err(
                ProviderError(
                    "Invalid prepared LiteLLM completion authority",
                    provider=self._extract_provider(prepared.config.model),
                )
            )
        if dispatch.state.dispatch_started:
            return Result.err(
                ProviderError(
                    "Prepared LiteLLM completion authority is single-use",
                    provider=self._extract_provider(prepared.config.model),
                )
            )

        dispatch.state.dispatch_started = True
        _register_proof_authority_audit(dispatch.state)
        try:
            return await self._complete_in_isolated_worker(
                messages,
                prepared.config,
                dispatch,
            )
        finally:
            _unregister_proof_authority_audit(dispatch.state)
            if dispatch.dependency_authority != self._dependency_authority(
                prepared.config.model,
                api_key=dispatch.api_key,
            ):
                dispatch.state.authority_drifted = True
            dispatch.state.cleanup_complete = True

    def _load_credentials_config(self):
        """Load credentials.yaml once, caching missing-config cases."""
        if self._credentials_cache is not _CREDENTIALS_UNSET:
            return self._credentials_cache

        try:
            from ouroboros.config import load_credentials
            from ouroboros.core.errors import ConfigError

            self._credentials_cache = load_credentials()
        except ConfigError:
            self._credentials_cache = None
        return self._credentials_cache

    def _get_configured_provider_credentials(self, model: str):
        """Load provider credentials for a model from credentials.yaml."""
        credentials = self._load_credentials_config()
        if credentials is None:
            return None

        provider_name = self._extract_provider(model)
        return credentials.providers.get(provider_name)

    @staticmethod
    def _normalize_api_key(value: str | None) -> str | None:
        """Treat blank and template placeholder API keys as unset."""
        if value is None:
            return None

        candidate = value.strip()
        if not candidate:
            return None
        if candidate.startswith(_PLACEHOLDER_API_KEY_PREFIX) and candidate.endswith(
            _PLACEHOLDER_API_KEY_SUFFIX
        ):
            return None
        return candidate

    def _get_api_key(self, model: str) -> str | None:
        """Get the appropriate API key for the model.

        Priority:
        1. Explicit api_key from constructor
        2. Environment variables based on model prefix
        3. credentials.yaml provider entry

        Args:
            model: The model identifier.

        Returns:
            The API key or None if not found.
        """
        explicit_api_key = self._normalize_api_key(self._api_key)
        if explicit_api_key:
            return explicit_api_key

        # Check environment variables based on model prefix
        if model.startswith("openrouter/"):
            env_key = self._normalize_api_key(os.environ.get("OPENROUTER_API_KEY"))
            if env_key:
                return env_key
        if model.startswith("anthropic/") or model.startswith("claude"):
            env_key = self._normalize_api_key(os.environ.get("ANTHROPIC_API_KEY"))
            if env_key:
                return env_key
        if model.startswith("openai/") or model.startswith("gpt"):
            env_key = self._normalize_api_key(os.environ.get("OPENAI_API_KEY"))
            if env_key:
                return env_key
        if model.startswith("google/") or model.startswith("gemini"):
            env_key = self._normalize_api_key(os.environ.get("GOOGLE_API_KEY"))
            if env_key:
                return env_key

        configured = self._get_configured_provider_credentials(model)
        if configured is not None:
            configured_api_key = self._normalize_api_key(configured.api_key)
            if configured_api_key:
                return configured_api_key

        # Unknown/custom models may still be routed through OpenRouter via credentials.
        provider_name = self._extract_provider(model)
        if provider_name not in {"openrouter", "openai", "anthropic", "google"}:
            credentials = self._load_credentials_config()
            configured = (
                credentials.providers.get("openrouter") if credentials is not None else None
            )
            if configured is not None:
                configured_api_key = self._normalize_api_key(configured.api_key)
                if configured_api_key:
                    return configured_api_key

        # Default to OpenRouter for unknown models
        return self._normalize_api_key(os.environ.get("OPENROUTER_API_KEY"))

    def _get_api_base(self, model: str) -> str | None:
        """Get the appropriate API base URL for the model."""
        if self._api_base:
            return self._api_base

        configured = self._get_configured_provider_credentials(model)
        if configured is not None:
            configured_base = self._normalize_api_base(configured.base_url)
            if configured_base:
                return configured_base

        if self._frugality_proof:
            global_base = self._normalize_api_base(getattr(litellm, "api_base", None))
            if global_base:
                return global_base
            provider = self._extract_provider(model)
            for name in _PROOF_API_BASE_ENVIRONMENT.get(provider, ()):
                environment_base = self._normalize_api_base(os.environ.get(name))
                if environment_base:
                    return environment_base
            return _PROOF_DEFAULT_API_BASES.get(provider)

        return None

    def _dependency_authority(
        self,
        model: str,
        *,
        api_key: str | None,
    ) -> _LiteLLMDependencyAuthority:
        """Accept only a bounded provider running with verified default globals."""
        provider = self._extract_provider(model)
        provider_config = _PROOF_PROVIDER_CONFIG_DEFAULTS.get(provider)
        config_is_default = False
        if provider_config is not None:
            config_name, expected = provider_config
            config_type = getattr(litellm, config_name, None)
            get_config = getattr(config_type, "get_config", None)
            try:
                config_is_default = callable(get_config) and get_config() == expected
            except Exception:  # noqa: BLE001 — opaque dependency state fails closed
                config_is_default = False
        provider_sdk_is_default = True
        if provider in {"openai", "openrouter"}:
            openai_module = getattr(litellm, "openai", None)
            provider_sdk_is_default = openai_module is not None and all(
                getattr(openai_module, name, None) is None
                for name in (
                    "api_key",
                    "api_version",
                    "base_url",
                    "default_headers",
                    "organization",
                    "project",
                )
            )
        defaults_verified = bool(
            _LITELLM_DEPENDENCY_VERSION
            and api_key
            and provider in _PROOF_UNSEALED_PROVIDER_ENVIRONMENT
            and all(getattr(litellm, name, None) is None for name in _PROOF_NONE_GLOBALS)
            and all(getattr(litellm, name, None) is False for name in _PROOF_FALSE_GLOBALS)
            and all(getattr(litellm, name, None) is True for name in _PROOF_TRUE_GLOBALS)
            and all(getattr(litellm, name, None) == [] for name in _PROOF_EMPTY_SEQUENCE_GLOBALS)
            and all(getattr(litellm, name, None) == {} for name in _PROOF_EMPTY_MAPPING_GLOBALS)
            and all(
                os.environ.get(name) in (None, "")
                for name in _PROOF_UNSEALED_PROVIDER_ENVIRONMENT.get(provider, ())
            )
            and all(
                os.environ.get(name) in (None, "") for name in _PROOF_UNSEALED_TRANSPORT_ENVIRONMENT
            )
            and config_is_default
            and provider_sdk_is_default
        )
        return _LiteLLMDependencyAuthority(
            version=_LITELLM_DEPENDENCY_VERSION,
            provider=provider,
            defaults_verified=defaults_verified,
        )

    @staticmethod
    def _sealed_httpx_client(timeout: float | None) -> httpx.AsyncClient:
        """Build the exact transport consumed by one proof-mode dispatch."""
        transport = httpx.AsyncHTTPTransport(
            verify=True,
            cert=None,
            trust_env=False,
            http1=True,
            http2=False,
            proxy=None,
            uds=None,
            local_address=None,
            retries=0,
        )
        return httpx.AsyncClient(
            verify=True,
            cert=None,
            trust_env=False,
            http1=True,
            http2=False,
            proxy=None,
            timeout=timeout,
            follow_redirects=True,
            transport=transport,
            headers={"User-Agent": f"litellm/{_LITELLM_DEPENDENCY_VERSION}"},
        )

    def _create_sealed_client(
        self,
        *,
        provider: str,
        api_key: str | None,
        api_base: str | None,
        timeout: float | None,
    ) -> _SealedLiteLLMClient | None:
        """Create a provider-compatible client without consulting mutable authority."""
        if provider not in _PROOF_DEFAULT_API_BASES or not api_key or not api_base:
            return None
        try:
            http_client = self._sealed_httpx_client(timeout)
            if provider == "openai":
                return _SealedLiteLLMClient(
                    value=openai.AsyncOpenAI(
                        api_key=api_key,
                        base_url=api_base,
                        timeout=timeout,
                        max_retries=_PROOF_LITELLM_MAX_RETRIES,
                        http_client=http_client,
                    ),
                    client_type=_PROOF_OPENAI_CLIENT_TYPE,
                )
            return _SealedLiteLLMClient(
                value=_SealedAsyncHTTPHandler(http_client, timeout=timeout),
                client_type=_PROOF_HTTP_HANDLER_CLIENT_TYPE,
            )
        except Exception:  # noqa: BLE001 — unavailable sealing fails proof closed
            return None

    @staticmethod
    def _proof_client_type(provider: str) -> str | None:
        if provider == "openai":
            return _PROOF_OPENAI_CLIENT_TYPE
        if provider in {"anthropic", "google", "openrouter"}:
            return _PROOF_HTTP_HANDLER_CLIENT_TYPE
        return None

    async def _complete_in_isolated_worker(
        self,
        messages: list[Message],
        config: CompletionConfig,
        dispatch: _PreparedLiteLLMDispatch,
    ) -> Result[CompletionResponse, ProviderError]:
        """Run LiteLLM beyond the mutable parent-process effect boundary."""
        recorder = get_current_io_journal_recorder() or self._io_recorder
        if recorder is None or not recorder.is_active:
            return await self._run_isolated_completion(messages, config, dispatch)

        request_options = _request_options_for_hash(config)
        prompt_text = _serialise_messages_for_hash(messages, request_options)
        async with recorder.record_llm_call(
            model_id=config.model,
            prompt_text=prompt_text,
            caller="litellm_adapter",
            max_tokens=config.max_tokens,
            temperature=config.temperature,
            extra=request_options,
        ) as call:
            result = await self._run_isolated_completion(messages, config, dispatch)
            if result.is_ok:
                _record_litellm_completion(call, result.value)
        return result

    async def _run_isolated_completion(
        self,
        messages: list[Message],
        config: CompletionConfig,
        dispatch: _PreparedLiteLLMDispatch,
    ) -> Result[CompletionResponse, ProviderError]:
        """Exchange one bounded request with a clean, single-purpose worker."""
        kwargs = self._build_completion_kwargs(
            messages,
            config,
            _sealed_dispatch=dispatch,
        )
        payload = json.dumps(
            {
                "protocol": _PROOF_WORKER_PROTOCOL,
                "client_type": dispatch.client_type,
                "completion_kwargs": kwargs,
                "config": {
                    "model": config.model,
                    "temperature": config.temperature,
                    "max_tokens": config.max_tokens,
                    "stop": config.stop,
                    "top_p": config.top_p,
                    "response_format": config.response_format,
                    "role": None,
                    "profile": None,
                    "max_turns": config.max_turns,
                    "reasoning_effort": config.reasoning_effort,
                    "model_is_explicit": config.model_is_explicit,
                },
            },
            separators=(",", ":"),
        ).encode("utf-8")
        worker_environment = {
            "PYTHONIOENCODING": "utf-8",
            "PYTHONUTF8": "1",
        }
        process: asyncio.subprocess.Process | None = None
        if dispatch.worker_authority != _worker_runtime_authority(
            dispatch.worker_authority.launcher_path
        ):
            dispatch.state.authority_drifted = True
        worker_directory = tempfile.TemporaryDirectory(prefix="ouroboros-litellm-proof-")
        try:
            process = await asyncio.create_subprocess_exec(
                dispatch.worker_authority.launcher_path or "",
                "-I",
                "-m",
                "ouroboros.providers.litellm_proof_worker",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                env=worker_environment,
                cwd=worker_directory.name,
            )
            timeout = (
                dispatch.timeout
                if isinstance(dispatch.timeout, int | float)
                and math.isfinite(dispatch.timeout)
                and dispatch.timeout > 0
                else 60.0
            )
            stdout, _ = await asyncio.wait_for(
                process.communicate(payload),
                timeout=timeout + 10.0,
            )
        except asyncio.CancelledError:
            if process is not None and process.returncode is None:
                process.kill()
                await process.wait()
            raise
        except Exception as exc:  # noqa: BLE001 — provider failures become Result errors
            if process is not None and process.returncode is None:
                process.kill()
                await process.wait()
            dispatch.state.cleanup_failed = True
            return Result.err(
                ProviderError.from_exception(
                    exc,
                    provider=self._extract_provider(config.model),
                )
            )
        finally:
            try:
                worker_directory.cleanup()
            except Exception:  # noqa: BLE001 — cleanup invalidates proof only
                dispatch.state.cleanup_failed = True

        if process.returncode != 0 or len(stdout) > _PROOF_WORKER_MAX_OUTPUT_BYTES:
            dispatch.state.cleanup_failed = True
            return Result.err(
                ProviderError(
                    "Isolated LiteLLM worker failed",
                    provider=self._extract_provider(config.model),
                )
            )
        try:
            report = json.loads(stdout)
            if not isinstance(report, dict) or set(report) != {"ok", "proof", "response"}:
                raise ValueError("invalid worker report")
            proof = report["proof"]
            proof_keys = {
                "authority_verified",
                "client_cleanup_succeeded",
                "client_type",
                "httpx_dependency_version",
                "litellm_dependency_version",
                "openai_dependency_version",
                "transport_contract",
                "worker_executable_resolved_path",
                "worker_executable_content_sha256",
                "worker_python_version",
                "worker_module",
                "worker_implementation_sha256",
                "adapter_module",
                "adapter_implementation_sha256",
                "ouroboros_implementation_contract",
                "ouroboros_implementation_sha256",
            }
            proof_is_valid = isinstance(proof, dict) and set(proof) == proof_keys
            if not proof_is_valid:
                dispatch.state.authority_drifted = True
                dispatch.state.cleanup_failed = True
            else:
                assert isinstance(proof, dict)
                if (
                    proof["authority_verified"] is not True
                    or proof["client_type"] != dispatch.client_type
                    or proof["transport_contract"] != _PROOF_TRANSPORT_CONTRACT
                    or proof["httpx_dependency_version"] != _HTTPX_DEPENDENCY_VERSION
                    or proof["litellm_dependency_version"] != _LITELLM_DEPENDENCY_VERSION
                    or proof["openai_dependency_version"] != _OPENAI_DEPENDENCY_VERSION
                    or proof["worker_executable_resolved_path"]
                    != dispatch.worker_authority.resolved_executable_path
                    or proof["worker_executable_content_sha256"]
                    != dispatch.worker_authority.executable_content_sha256
                    or proof["worker_python_version"] != dispatch.worker_authority.python_version
                    or proof["worker_module"] != dispatch.worker_authority.worker_module
                    or proof["worker_implementation_sha256"]
                    != dispatch.worker_authority.worker_implementation_sha256
                    or proof["adapter_module"] != dispatch.worker_authority.adapter_module
                    or proof["adapter_implementation_sha256"]
                    != dispatch.worker_authority.adapter_implementation_sha256
                    or proof["ouroboros_implementation_contract"]
                    != dispatch.worker_authority.ouroboros_implementation_contract
                    or proof["ouroboros_implementation_sha256"]
                    != dispatch.worker_authority.ouroboros_implementation_sha256
                ):
                    dispatch.state.authority_drifted = True
                if proof["client_cleanup_succeeded"] is not True:
                    dispatch.state.cleanup_failed = True
            if report["ok"] is not True:
                return Result.err(
                    ProviderError(
                        "Isolated LiteLLM completion failed",
                        provider=self._extract_provider(config.model),
                    )
                )
            response = report["response"]
            if not isinstance(response, dict) or set(response) != {
                "content",
                "finish_reason",
                "model",
                "usage",
            }:
                raise ValueError("invalid worker response")
            usage = response["usage"]
            if not isinstance(usage, dict) or set(usage) != {
                "completion_tokens",
                "prompt_tokens",
                "total_tokens",
            }:
                raise ValueError("invalid worker usage")
            counters = tuple(
                usage[name] for name in ("prompt_tokens", "completion_tokens", "total_tokens")
            )
            if not all(type(value) is int and value >= 0 for value in counters):
                raise ValueError("invalid worker counters")
            prompt_tokens, completion_tokens, total_tokens = counters
            if total_tokens != prompt_tokens + completion_tokens:
                raise ValueError("inconsistent worker counters")
            if not all(
                isinstance(response[name], str) for name in ("content", "finish_reason", "model")
            ):
                raise ValueError("invalid worker text")
            return Result.ok(
                CompletionResponse(
                    content=response["content"],
                    model=response["model"],
                    usage=UsageInfo(
                        prompt_tokens=prompt_tokens,
                        completion_tokens=completion_tokens,
                        total_tokens=total_tokens,
                    ),
                    finish_reason=response["finish_reason"],
                )
            )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            dispatch.state.authority_drifted = True
            return Result.err(
                ProviderError.from_exception(
                    exc,
                    provider=self._extract_provider(config.model),
                )
            )

    @staticmethod
    def _normalize_api_base(value: object) -> str | None:
        return value.strip() if isinstance(value, str) and value.strip() else None

    def _build_completion_kwargs(
        self,
        messages: list[Message],
        config: CompletionConfig,
        *,
        _sealed_dispatch: _PreparedLiteLLMDispatch | None = None,
    ) -> dict[str, Any]:
        """Build the kwargs for litellm.acompletion.

        Args:
            messages: The conversation messages.
            config: The completion configuration.

        Returns:
            Dictionary of kwargs for litellm.acompletion.
        """
        kwargs: dict[str, Any] = {
            "model": config.model,
            "messages": [m.to_dict() for m in messages],
            "temperature": config.temperature,
            "max_tokens": config.max_tokens,
            "timeout": (
                _sealed_dispatch.timeout if _sealed_dispatch is not None else self._timeout
            ),
        }
        if _sealed_dispatch is not None:
            kwargs["max_retries"] = _PROOF_LITELLM_MAX_RETRIES

        # Anthropic models don't accept both temperature and top_p together
        # Other providers (OpenAI, OpenRouter) support both
        model_lower = config.model.lower()
        if not ("anthropic" in model_lower or "claude" in model_lower):
            kwargs["top_p"] = config.top_p

        if config.stop:
            kwargs["stop"] = config.stop

        if config.response_format:
            kwargs["response_format"] = config.response_format

        # Effort-first investment dial (RFC #1405). LiteLLM forwards
        # ``reasoning_effort`` to each provider's native knob (Anthropic's
        # output_config.effort, OpenAI-style reasoning_effort, etc.), so this is
        # the provider-agnostic path. Omitted when unset to preserve behavior.
        if config.reasoning_effort:
            kwargs["reasoning_effort"] = config.reasoning_effort

        api_key = (
            _sealed_dispatch.api_key
            if _sealed_dispatch is not None
            else self._get_api_key(config.model)
        )
        if api_key:
            kwargs["api_key"] = api_key

        api_base = (
            _sealed_dispatch.api_base
            if _sealed_dispatch is not None
            else self._get_api_base(config.model)
        )
        if api_base:
            kwargs["api_base"] = api_base

        return kwargs

    async def _raw_complete(
        self,
        messages: list[Message],
        config: CompletionConfig,
        *,
        _sealed_dispatch: _PreparedLiteLLMDispatch | None = None,
    ) -> litellm.ModelResponse:
        """Make the raw completion call.

        Args:
            messages: The conversation messages.
            config: The completion configuration.

        Returns:
            The raw LiteLLM response.

        Raises:
            litellm exceptions for API errors.
        """
        kwargs = self._build_completion_kwargs(
            messages,
            config,
            _sealed_dispatch=_sealed_dispatch,
        )

        log.debug(
            "llm.request.started",
            model=config.model,
            message_count=len(messages),
            temperature=config.temperature,
            max_tokens=config.max_tokens,
        )

        response = await litellm.acompletion(**kwargs)

        log.debug(
            "llm.request.completed",
            model=config.model,
            finish_reason=response.choices[0].finish_reason,
        )

        return response

    def _parse_response(
        self,
        response: litellm.ModelResponse,
        config: CompletionConfig,
    ) -> CompletionResponse:
        """Parse the LiteLLM response into CompletionResponse.

        Args:
            response: The raw LiteLLM response.
            config: The completion configuration.

        Returns:
            Parsed CompletionResponse.
        """
        choice = response.choices[0]
        usage = response.usage
        content = choice.message.content or ""

        # Security: Validate LLM response length to prevent DoS
        is_valid, error_msg = InputValidator.validate_llm_response(content)
        if not is_valid:
            log.warning(
                "llm.response.truncated",
                model=config.model,
                original_length=len(content),
                max_length=MAX_LLM_RESPONSE_LENGTH,
            )
            # Truncate oversized responses instead of failing
            content = content[:MAX_LLM_RESPONSE_LENGTH]

        return CompletionResponse(
            content=content,
            model=response.model or config.model,
            usage=UsageInfo(
                prompt_tokens=usage.prompt_tokens if usage else 0,
                completion_tokens=usage.completion_tokens if usage else 0,
                total_tokens=usage.total_tokens if usage else 0,
            ),
            finish_reason=choice.finish_reason or "stop",
            raw_response=response.model_dump() if hasattr(response, "model_dump") else {},
        )

    async def complete(
        self,
        messages: list[Message],
        config: CompletionConfig,
        *,
        _sealed_dispatch: _PreparedLiteLLMDispatch | None = None,
    ) -> Result[CompletionResponse, ProviderError]:
        """Make a completion request to the LLM provider.

        This method handles retries internally and converts
        all expected failures to Result.err(ProviderError).

        Args:
            messages: The conversation messages to send.
            config: Configuration for the completion request.

        Returns:
            Result containing either the completion response or a ProviderError.
        """
        if _sealed_dispatch is None:
            profile_result = resolve_completion_profile_result(config, backend="litellm")
            if profile_result.is_err:
                return Result.err(profile_result.error)
            config = profile_result.value.config
        elif _sealed_dispatch.config != config:
            return Result.err(
                ProviderError(
                    "Prepared LiteLLM config does not match sealed dispatch authority",
                    provider=self._extract_provider(config.model),
                )
            )

        max_retries = (
            _sealed_dispatch.max_retries if _sealed_dispatch is not None else self._max_retries
        )

        @retry_async(
            on=RETRIABLE_EXCEPTIONS,
            attempts=max_retries,
            wait_initial=1.0,
            wait_max=10.0,
            wait_jitter=1.0,
        )
        async def _with_retry() -> CompletionResponse:
            recorder = get_current_io_journal_recorder() or self._io_recorder
            if recorder is None or not recorder.is_active:
                response = await self._raw_complete(
                    messages,
                    config,
                    _sealed_dispatch=_sealed_dispatch,
                )
                return self._parse_response(response, config)
            request_options = _request_options_for_hash(config)
            prompt_text = _serialise_messages_for_hash(messages, request_options)
            async with recorder.record_llm_call(
                model_id=config.model,
                prompt_text=prompt_text,
                caller="litellm_adapter",
                max_tokens=config.max_tokens,
                temperature=config.temperature,
                extra=request_options,
            ) as call:
                response = await self._raw_complete(
                    messages,
                    config,
                    _sealed_dispatch=_sealed_dispatch,
                )
                parsed = self._parse_response(response, config)
                _record_litellm_completion(call, parsed)
            return parsed

        try:
            parsed = await _with_retry()
            return Result.ok(parsed)
        except RETRIABLE_EXCEPTIONS as e:
            # All retries exhausted
            log.warning(
                "llm.request.failed.retries_exhausted",
                model=config.model,
                error=str(e),
                max_retries=max_retries,
            )
            return Result.err(
                ProviderError.from_exception(e, provider=self._extract_provider(config.model))
            )
        except litellm.APIError as e:
            # Non-retriable API error
            log.warning(
                "llm.request.failed.api_error",
                model=config.model,
                error=str(e),
                status_code=getattr(e, "status_code", None),
            )
            return Result.err(
                ProviderError.from_exception(e, provider=self._extract_provider(config.model))
            )
        except litellm.AuthenticationError as e:
            log.warning(
                "llm.request.failed.auth_error",
                model=config.model,
                error=str(e),
            )
            return Result.err(
                ProviderError(
                    "Authentication failed - check API key",
                    provider=self._extract_provider(config.model),
                    status_code=401,
                    details={"original_exception": type(e).__name__},
                )
            )
        except litellm.BadRequestError as e:
            log.warning(
                "llm.request.failed.bad_request",
                model=config.model,
                error=str(e),
            )
            return Result.err(
                ProviderError.from_exception(e, provider=self._extract_provider(config.model))
            )
        except Exception as e:
            # Unexpected error - log and convert to ProviderError
            log.exception(
                "llm.request.failed.unexpected",
                model=config.model,
                error=str(e),
            )
            return Result.err(
                ProviderError(
                    f"Unexpected error: {e!s}",
                    provider=self._extract_provider(config.model),
                    details={"original_exception": type(e).__name__},
                )
            )

    def _extract_provider(self, model: str) -> str:
        """Extract the provider name from a model string.

        Args:
            model: The model identifier (e.g., 'openrouter/openai/gpt-4').

        Returns:
            The provider name (e.g., 'openrouter').
        """
        if "/" in model:
            return model.split("/")[0]
        # Common model prefixes
        if (
            model.startswith("gpt")
            or model.startswith("o1")
            or model.startswith("o3")
            or model.startswith("o4")
        ):
            return "openai"
        if model.startswith("claude"):
            return "anthropic"
        if model.startswith("gemini"):
            return "google"
        return "unknown"
