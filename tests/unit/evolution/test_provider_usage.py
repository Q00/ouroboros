"""Generation provider calls are measured completely or fail closed."""

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass, replace
from dataclasses import field as dataclass_field
import json
import os
from pathlib import Path
import shutil
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from ouroboros.config.models import OuroborosConfig
from ouroboros.core.errors import ProviderError
from ouroboros.core.types import Result
from ouroboros.evolution import provider_usage as provider_usage_module
from ouroboros.evolution.focus import call_executor
from ouroboros.evolution.provider_usage import (
    capture_generation_provider_usage,
    observe_callable,
    tracked_agent_task,
    tracked_agent_task_to_result,
    tracked_complete,
)
from ouroboros.orchestrator.codex_cli_runtime import CodexCliRuntime
import ouroboros.orchestrator.frugality_runtime_attestation as frugality_attestation_module
from ouroboros.orchestrator.frugality_runtime_attestation import (
    codex_child_environment_attestation,
)
from ouroboros.providers.anthropic_adapter import AnthropicAdapter
from ouroboros.providers.base import CompletionConfig, CompletionResponse, UsageInfo
import ouroboros.providers.credential_authority as credential_authority_module
from ouroboros.providers.credential_authority import (
    installation_authority_key as _real_installation_authority_key,
)
from ouroboros.providers.frugality_attestation import (
    PreparedFrugalityCompletion,
    credential_authority_digest,
    effective_completion_request,
)
from ouroboros.providers.profiles import (
    ResolvedCompletionProfile,
    resolve_completion_profile_result,
)


@dataclass
class _Adapter:
    tokens: int | None
    unallocated_tokens: int = 0
    response_model: str = "test-model"
    llm_backend: str | None = None
    api_base: str = "https://provider.example/v1"
    timeout: float = 30.0
    max_retries: int = 1
    permission_mode: str = "read-only"
    allowed_tools: tuple[str, ...] = ("Read",)
    runtime_profile: str = "worker"
    resolve_profiles: bool = False
    dispatched: list[CompletionConfig] = dataclass_field(default_factory=list)
    contract_override: Mapping[str, object] | None = None

    def _contract(self, resolved: ResolvedCompletionProfile) -> Mapping[str, object]:
        if self.contract_override is not None:
            return self.contract_override
        return {
            "schema_version": 1,
            "schema_id": "test.adapter.v1",
            "internal_attempt_limit": self.max_retries,
            "credential_authority": credential_authority_digest("test-credential"),
            "effective_request": effective_completion_request(
                resolved,
                provider_model=resolved.config.model,
            ),
            "settings": {
                "api_base": self.api_base,
                "timeout": self.timeout,
                "max_retries": self.max_retries,
                "io_recorder_configured": False,
                "permission_mode": self.permission_mode,
                "allowed_tools": self.allowed_tools,
                "runtime_profile": self.runtime_profile,
            },
        }

    def frugality_prepare_completion(
        self,
        config: CompletionConfig,
    ) -> Result[PreparedFrugalityCompletion, ProviderError]:
        if self.resolve_profiles:
            profile_result = resolve_completion_profile_result(config, backend="litellm")
            if profile_result.is_err:
                return Result.err(profile_result.error)
            resolved = profile_result.value
        else:
            resolved = ResolvedCompletionProfile(config=config)
        return Result.ok(
            PreparedFrugalityCompletion(
                config=replace(resolved.config, role=None, profile=None),
                contract=self._contract(resolved),
            )
        )

    async def complete(self, messages: object, config: CompletionConfig):  # noqa: ANN201, ARG002
        self.dispatched.append(config)
        if self.tokens is None:
            return Result.err(ProviderError(message="provider failed", provider="test"))
        return Result.ok(
            CompletionResponse(
                content="{}",
                model=self.response_model,
                usage=UsageInfo(
                    prompt_tokens=self.tokens,
                    completion_tokens=0,
                    total_tokens=self.tokens,
                    unallocated_tokens=self.unallocated_tokens,
                ),
            )
        )


class _AttestedRuntime:
    runtime_backend = "test-runtime-handle"
    _runtime_backend = "test-runtime"
    llm_backend = "test-provider"
    _model = "test-model"
    permission_mode = "acceptEdits"

    def __init__(
        self,
        *,
        usage: Mapping[str, object] | None = None,
        effective_model: object = "test-model",
    ) -> None:
        self.usage = dict(usage or {"total_tokens": 150})
        self.effective_model = effective_model

    def frugality_runtime_attestation(self) -> Mapping[str, object]:
        runtime_class = f"{type(self).__module__}.{type(self).__qualname__}"
        return {
            "schema_version": 1,
            "schema_id": "test.agent_runtime.v1",
            "implementation": runtime_class,
            "runtime_backend": "test-runtime",
            "runtime_handle_backend": "test-runtime-handle",
            "settings": {"runtime_version": "v1"},
        }

    def _message(self) -> SimpleNamespace:
        data: dict[str, object] = {"usage": self.usage}
        if self.effective_model is not None:
            data["model_observation"] = {"effective_model": self.effective_model}
        return SimpleNamespace(data=data)

    async def execute_task(self, **kwargs):  # noqa: ANN201, ARG002
        yield self._message()

    async def execute_task_to_result(self, **kwargs):  # noqa: ANN201, ARG002
        return Result.ok(SimpleNamespace(messages=(self._message(),)))


def _write_fake_codex_cli(path: Path, *, version: str) -> Path:
    path.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = "--version" ]; then\n'
        f"  echo 'codex {version}'\n"
        "  exit 0\n"
        "fi\n"
        "exit 0\n",
        encoding="utf-8",
    )
    path.chmod(0o755)
    return path


async def _codex_runtime_summary(runtime: CodexCliRuntime):  # noqa: ANN202
    async def _execute_task(**kwargs):  # noqa: ANN201, ARG001
        yield SimpleNamespace(
            data={
                "model_observation": {"effective_model": "test-model"},
                "usage": {"total_tokens": 100},
            }
        )

    runtime.execute_task = _execute_task  # type: ignore[method-assign]
    with capture_generation_provider_usage() as capture:
        _ = [
            message
            async for message in tracked_agent_task(
                runtime,
                role="executor_auxiliary",
                prompt="analyze",
            )
        ]
    return capture.summary()


@pytest.fixture(autouse=True)
def _answer_fake_cli_version_probes_without_spawning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Serve ``--version`` for the fake codex CLIs by parsing the script.

    Every ``CodexCliRuntime`` attestation probe spawns ``<cli> --version``
    (~45ms of subprocess wait each, dozens per fingerprint test). The fake
    CLIs written by :func:`_write_fake_codex_cli` only echo a constant, so
    answering from the script text preserves the probed identity — including
    the version drift between fixtures — without the process spawns. Any
    other invocation (real binaries, failure-shape fixtures) still executes.
    """
    import re
    import subprocess

    from ouroboros.orchestrator import cli_version_attestation

    real_run = subprocess.run

    def _run(args, **kwargs):  # noqa: ANN001, ANN202
        if isinstance(args, list) and len(args) == 2 and args[1] == "--version":
            try:
                script = Path(args[0]).read_text(encoding="utf-8")
            except (OSError, UnicodeError):
                script = ""
            match = re.search(r"echo 'codex ([^']+)'", script)
            if match is not None:
                return subprocess.CompletedProcess(
                    args, 0, stdout=f"codex {match.group(1)}\n", stderr=""
                )
        return real_run(args, **kwargs)

    monkeypatch.setattr(cli_version_attestation.subprocess, "run", _run)


@pytest.fixture(autouse=True)
def _resolve_project_identity_without_git(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the attestation's semantic-cwd resolution to a constant.

    ``resolve_project_identity`` shells out to git several times per
    ``CodexCliRuntime`` construction; the fingerprint tests here only need
    the resolved ``workspace_path`` to be deterministic and identical across
    variants. Tests that exercise cwd-relative identity re-patch this and
    win (see the repository-relative cwd test below).
    """
    monkeypatch.setattr(
        frugality_attestation_module,
        "resolve_project_identity",
        lambda _cwd: SimpleNamespace(workspace_path="."),
    )


@pytest.fixture(autouse=True)
def _register_test_completion_schema(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        frugality_attestation_module,
        "installation_authority_key",
        lambda: b"i" * 32,
    )
    monkeypatch.setattr(
        credential_authority_module,
        "installation_authority_key",
        lambda: b"i" * 32,
    )
    adapter_class = f"{_Adapter.__module__}.{_Adapter.__qualname__}"
    monkeypatch.setitem(
        provider_usage_module._COMPLETION_ADAPTER_SCHEMAS,
        adapter_class,
        provider_usage_module._CompletionAdapterSchema(
            schema_id="test.adapter.v1",
            settings=frozenset(
                {
                    "api_base",
                    "timeout",
                    "max_retries",
                    "io_recorder_configured",
                    "permission_mode",
                    "allowed_tools",
                    "runtime_profile",
                }
            ),
            retry_setting="max_retries",
            proof_retry_value=1,
        ),
    )
    runtime_class = f"{_AttestedRuntime.__module__}.{_AttestedRuntime.__qualname__}"
    monkeypatch.setitem(
        provider_usage_module._AGENT_RUNTIME_SCHEMAS,
        runtime_class,
        provider_usage_module._AgentRuntimeSchema(
            schema_id="test.agent_runtime.v1",
            runtime_backend="test-runtime",
            runtime_handle_backend="test-runtime-handle",
            setting_kinds={"runtime_version": "text"},
        ),
    )


async def test_generation_provider_usage_sums_every_tracked_call() -> None:
    config = CompletionConfig(model="test-model", role="wonder")
    with capture_generation_provider_usage() as capture:
        await tracked_complete(_Adapter(30), [], config)
        await tracked_complete(_Adapter(20), [], config)

    summary = capture.summary(instrumentation_complete=True)

    assert summary.complete is True
    assert summary.call_count == 2
    assert summary.token_spend == 50
    assert summary.identity_digest is not None
    assert summary.configuration_fingerprint is not None
    assert summary.configuration_assignments
    assert summary.issues == ()


@pytest.mark.parametrize(
    ("allocated_tokens", "unallocated_tokens"),
    [
        pytest.param(0, 132, id="total-only"),
        pytest.param(120, 12, id="cache-inclusive"),
    ],
)
async def test_generation_provider_usage_accounts_explicit_unallocated_authority(
    allocated_tokens: int,
    unallocated_tokens: int,
) -> None:
    config = CompletionConfig(model="test-model", role="reflect")
    with capture_generation_provider_usage() as capture:
        await tracked_complete(
            _Adapter(allocated_tokens, unallocated_tokens=unallocated_tokens),
            [],
            config,
        )

    summary = capture.summary(instrumentation_complete=True)

    assert summary.complete is True
    assert summary.token_spend == 132
    assert summary.issues == ()


@pytest.mark.parametrize(
    "unallocated_tokens",
    [
        pytest.param(True, id="boolean"),
        pytest.param(-1, id="negative"),
        pytest.param(10**400, id="overflow"),
    ],
)
async def test_generation_provider_usage_rejects_invalid_unallocated_authority(
    unallocated_tokens: int,
) -> None:
    config = CompletionConfig(model="test-model", role="reflect")
    with capture_generation_provider_usage() as capture:
        await tracked_complete(
            _Adapter(0, unallocated_tokens=unallocated_tokens),
            [],
            config,
        )

    summary = capture.summary(instrumentation_complete=True)

    assert summary.complete is False
    assert summary.token_spend is None
    assert "missing or invalid" in "; ".join(summary.issues)


async def test_generation_provider_usage_rejects_any_missing_runtime_usage() -> None:
    config = CompletionConfig(model="test-model", role="reflect")
    with capture_generation_provider_usage() as capture:
        await tracked_complete(_Adapter(20), [], config)
        await tracked_complete(_Adapter(None), [], config)

    summary = capture.summary(instrumentation_complete=True)

    assert summary.complete is False
    assert summary.call_count == 2
    assert summary.token_spend is None
    assert "missing or invalid" in "; ".join(summary.issues)


async def test_completion_usage_counters_must_reconcile_exactly() -> None:
    class _InconsistentAdapter:
        async def complete(self, messages: object, config: object):  # noqa: ANN201, ARG002
            return Result.ok(
                CompletionResponse(
                    content="{}",
                    model="test-model",
                    usage=UsageInfo(
                        prompt_tokens=100,
                        completion_tokens=50,
                        total_tokens=1,
                    ),
                )
            )

    with capture_generation_provider_usage() as capture:
        await tracked_complete(
            _InconsistentAdapter(),
            [],
            CompletionConfig(model="test-model", role="reflect"),
        )

    summary = capture.summary()
    assert summary.complete is False
    assert summary.token_spend is None
    assert "missing or invalid" in "; ".join(summary.issues)


async def test_generation_provider_usage_rejects_overflowing_runtime_usage() -> None:
    config = CompletionConfig(model="test-model", role="reflect")
    with capture_generation_provider_usage() as capture:
        await tracked_complete(_Adapter(10**400), [], config)

    summary = capture.summary(instrumentation_complete=True)

    assert summary.complete is False
    assert summary.token_spend is None
    assert "missing or invalid" in "; ".join(summary.issues)


async def test_generation_provider_usage_rejects_non_finite_aggregate() -> None:
    config = CompletionConfig(model="test-model", role="reflect")
    with capture_generation_provider_usage() as capture:
        await tracked_complete(_Adapter(10**308), [], config)
        await tracked_complete(_Adapter(10**308), [], config)

    summary = capture.summary(instrumentation_complete=True)

    assert summary.complete is False
    assert summary.token_spend is None
    assert "total is non-finite" in "; ".join(summary.issues)


async def test_generation_provider_usage_is_bounded(monkeypatch) -> None:  # noqa: ANN001
    monkeypatch.setattr(provider_usage_module, "_MAX_PROVIDER_CALLS", 1)
    config = CompletionConfig(model="test-model", role="reflect")
    with capture_generation_provider_usage() as capture:
        await tracked_complete(_Adapter(10), [], config)
        await tracked_complete(_Adapter(10), [], config)

    summary = capture.summary(instrumentation_complete=True)

    assert summary.complete is False
    assert summary.call_count == 2
    assert summary.token_spend is None
    assert sum(len(values) for _, values in summary.configuration_assignments) == 1
    assert "bounded collection limit" in "; ".join(summary.issues)


def test_cyclic_provider_configuration_fails_closed_without_escaping() -> None:
    cyclic: dict[str, object] = {}
    cyclic["self"] = cyclic
    with capture_generation_provider_usage() as capture:
        call = capture.begin(role="reflect", model="test-model", configuration=cyclic)
        capture.finish(call, 10)

    summary = capture.summary()

    assert summary.complete is False
    assert summary.token_spend is None
    assert "instrumentation is incomplete" in "; ".join(summary.issues)


async def test_provider_configuration_fingerprint_tracks_model_and_effort() -> None:
    with capture_generation_provider_usage() as first:
        await tracked_complete(
            _Adapter(20),
            [],
            CompletionConfig(model="model-a", role="reflect", reasoning_effort="low"),
        )
    with capture_generation_provider_usage() as second:
        await tracked_complete(
            _Adapter(20),
            [],
            CompletionConfig(model="model-b", role="reflect", reasoning_effort="high"),
        )

    assert first.summary().configuration_fingerprint != second.summary().configuration_fingerprint


async def test_completion_adapter_contract_binds_instance_execution_settings() -> None:
    config = CompletionConfig(model="test-model", role="reflect")
    first_adapter = _Adapter(100)
    second_adapter = _Adapter(
        10,
        api_base="https://other.example/v1",
        timeout=90.0,
        permission_mode="acceptEdits",
        allowed_tools=("Read", "Write"),
        runtime_profile="frontier",
    )
    with capture_generation_provider_usage() as first:
        await tracked_complete(first_adapter, [], config)
    with capture_generation_provider_usage() as second:
        await tracked_complete(second_adapter, [], config)

    assert first.summary().complete is True
    assert second.summary().complete is True
    assert first.summary().configuration_fingerprint != second.summary().configuration_fingerprint
    assert first.summary().configuration_assignments != second.summary().configuration_assignments


async def test_unattested_or_retry_opaque_completion_adapter_fails_closed() -> None:
    class _UnattestedAdapter:
        async def complete(self, messages: object, config: object):  # noqa: ANN201, ARG002
            return await _Adapter(10).complete(messages, config)

    config = CompletionConfig(model="test-model", role="reflect")
    for adapter in (_UnattestedAdapter(), _Adapter(10, max_retries=2)):
        with capture_generation_provider_usage() as capture:
            await tracked_complete(adapter, [], config)

        summary = capture.summary()
        assert summary.complete is False
        assert summary.token_spend is None
        assert "instrumentation is incomplete" in "; ".join(summary.issues)


async def test_completion_adapter_contract_rejects_boolean_schema_or_empty_settings() -> None:
    config = CompletionConfig(model="test-model", role="reflect")
    base = dict(_Adapter(10)._contract(ResolvedCompletionProfile(config=config)))
    settings = dict(base["settings"])  # type: ignore[arg-type]
    contracts = (
        {**base, "schema_version": True},
        {**base, "settings": {}},
        {**base, "settings": {"timeout": 1.0}},
        {**base, "settings": {**settings, "api_key": "sk-super-secret"}},
        {**base, "settings": {**settings, "max_retries": 99}},
    )
    for contract in contracts:
        with capture_generation_provider_usage() as capture:
            await tracked_complete(
                _Adapter(10, contract_override=contract),
                [],
                config,
            )

        summary = capture.summary()
        assert summary.complete is False
        assert summary.token_spend is None
        assert "instrumentation is incomplete" in "; ".join(summary.issues)


async def test_cyclic_adapter_attestation_fails_closed_without_escaping() -> None:
    config = CompletionConfig(model="test-model", role="reflect")
    base = dict(_Adapter(10)._contract(ResolvedCompletionProfile(config=config)))
    cyclic_settings = dict(base["settings"])  # type: ignore[arg-type]
    cyclic_settings["cycle"] = cyclic_settings
    contract = {**base, "settings": cyclic_settings}

    with capture_generation_provider_usage() as capture:
        await tracked_complete(_Adapter(10, contract_override=contract), [], config)

    summary = capture.summary()
    assert summary.complete is False
    assert summary.token_spend is None
    assert "instrumentation is incomplete" in "; ".join(summary.issues)


async def test_shared_configuration_dag_is_not_misclassified_as_a_cycle() -> None:
    shared_schema = {
        "type": "json_schema",
        "json_schema": {"name": "result", "schema": {"type": "object"}},
    }
    config = CompletionConfig(
        model="test-model",
        role="semantic_evaluation",
        response_format=shared_schema,
    )

    with capture_generation_provider_usage() as capture:
        await tracked_complete(_Adapter(10), [], config)

    summary = capture.summary()
    assert summary.complete is True
    assert summary.token_spend == 10


async def test_unbounded_or_non_finite_adapter_attestation_fails_closed() -> None:
    config = CompletionConfig(model="test-model", role="reflect")
    base = dict(_Adapter(10)._contract(ResolvedCompletionProfile(config=config)))
    settings = dict(base["settings"])  # type: ignore[arg-type]
    nested: object = "leaf"
    for _ in range(provider_usage_module._MAX_CONFIGURATION_DEPTH + 1):
        nested = {"nested": nested}
    contracts = (
        {**base, "settings": {**settings, "runtime_profile": nested}},
        {
            **base,
            "settings": {
                **settings,
                "runtime_profile": "x" * (provider_usage_module._MAX_CONFIGURATION_TEXT_LENGTH + 1),
            },
        },
        {**base, "settings": {**settings, "timeout": float("inf")}},
        {**base, "credential_authority": "sk-plaintext-secret"},
    )

    for contract in contracts:
        with capture_generation_provider_usage() as capture:
            await tracked_complete(_Adapter(10, contract_override=contract), [], config)
        assert capture.summary().complete is False


async def test_completion_tracking_binds_and_dispatches_profile_resolved_config() -> None:
    dispatched: list[CompletionConfig] = []
    adapter = _Adapter(10, resolve_profiles=True, dispatched=dispatched)
    request = CompletionConfig(model="default", role="reflect", profile="proof")
    profiles = (
        OuroborosConfig(
            llm_profiles={"proof": {"model": "model-a", "temperature": 0.1, "max_tokens": 100}}
        ),
        OuroborosConfig(
            llm_profiles={"proof": {"model": "model-b", "temperature": 0.9, "max_tokens": 200}}
        ),
    )
    summaries = []
    for profile_config in profiles:
        with patch("ouroboros.providers.profiles.load_config", return_value=profile_config):
            with capture_generation_provider_usage() as capture:
                await tracked_complete(adapter, [], request)
        summaries.append(capture.summary())

    assert all(summary.complete for summary in summaries)
    assert summaries[0].configuration_assignments != summaries[1].configuration_assignments
    assert summaries[0].configuration_fingerprint != summaries[1].configuration_fingerprint
    assert [(config.model, config.temperature, config.max_tokens) for config in dispatched] == [
        ("model-a", 0.1, 100),
        ("model-b", 0.9, 200),
    ]
    assert all(config.role is None and config.profile is None for config in dispatched)


async def test_litellm_attestation_binds_post_profile_dispatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    litellm_adapter = pytest.importorskip("ouroboros.providers.litellm_adapter")
    LiteLLMAdapter = litellm_adapter.LiteLLMAdapter
    dispatched: list[CompletionConfig] = []
    adapter = LiteLLMAdapter(
        api_key="test-credential",
        api_base="https://provider.example/v1",
        max_retries=1,
    )

    async def _complete_prepared(
        messages: object,
        prepared: PreparedFrugalityCompletion,
    ) -> Result[CompletionResponse, ProviderError]:
        config = prepared.config
        dispatched.append(config)
        return Result.ok(
            CompletionResponse(
                content="{}",
                model=config.model,
                usage=UsageInfo(prompt_tokens=9, completion_tokens=1, total_tokens=10),
            )
        )

    monkeypatch.setattr(adapter, "frugality_complete_prepared", _complete_prepared)
    request = CompletionConfig(model="default", role="reflect", profile="proof")
    profiles = (
        OuroborosConfig(
            llm_profiles={
                "proof": {
                    "model": "openai/model-a",
                    "temperature": 0.1,
                    "max_tokens": 100,
                    "reasoning_effort": "low",
                }
            }
        ),
        OuroborosConfig(
            llm_profiles={
                "proof": {
                    "model": "openai/model-b",
                    "temperature": 0.9,
                    "max_tokens": 200,
                    "reasoning_effort": "high",
                }
            }
        ),
    )
    summaries = []
    for profile_config in profiles:
        with patch("ouroboros.providers.profiles.load_config", return_value=profile_config):
            with capture_generation_provider_usage() as capture:
                await tracked_complete(adapter, [], request)
        summaries.append(capture.summary())

    assert all(summary.complete for summary in summaries)
    assert summaries[0].configuration_assignments != summaries[1].configuration_assignments
    assert [(config.model, config.temperature, config.max_tokens) for config in dispatched] == [
        ("openai/model-a", 0.1, 100),
        ("openai/model-b", 0.9, 200),
    ]
    assert all(config.role is None and config.profile is None for config in dispatched)


def test_anthropic_attestation_requires_one_attempt_and_secret_safe_authority() -> None:
    client = SimpleNamespace(
        base_url="https://provider.example/v1",
        api_key="credential-a",
    )
    config = CompletionConfig(model="claude-sonnet-4-20250514", role="reflect")
    safe = AnthropicAdapter(api_key="credential-a", max_retries=0)
    unsafe = AnthropicAdapter(api_key="credential-a", max_retries=1)
    safe._client = client
    unsafe._client = client

    prepared_result = safe.frugality_prepare_completion(config)
    assert prepared_result.is_ok
    prepared = prepared_result.value
    normalized = provider_usage_module._completion_adapter_configuration(
        safe,
        prepared.contract,
    )
    assert normalized is not provider_usage_module._INVALID_CONFIGURATION
    assert "credential-a" not in repr(normalized)

    prepared_result = unsafe.frugality_prepare_completion(config)
    assert prepared_result.is_ok
    prepared = prepared_result.value
    assert (
        provider_usage_module._completion_adapter_configuration(
            unsafe,
            prepared.contract,
        )
        is provider_usage_module._INVALID_CONFIGURATION
    )


def test_anthropic_attestation_uses_cached_client_dispatch_authority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _CachedClient:
        base_url = "https://cached-client.example/v1/"
        api_key = "cached-client-credential"

    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://environment.example/v1")
    adapter = AnthropicAdapter(api_key="constructor-credential", max_retries=0)
    adapter._client = _CachedClient()

    prepared_result = adapter.frugality_prepare_completion(
        CompletionConfig(model="claude-sonnet-4-20250514", role="reflect")
    )

    assert prepared_result.is_ok
    contract = prepared_result.value.contract
    assert contract["credential_authority"] == credential_authority_digest(
        "cached-client-credential"
    )
    settings = contract["settings"]
    assert isinstance(settings, Mapping)
    assert settings["api_base"] == "https://cached-client.example/v1/"
    assert "environment.example" not in repr(contract)
    assert "cached-client-credential" not in repr(contract)


def test_litellm_attestation_requires_one_attempt_and_secret_safe_authority() -> None:
    litellm_adapter = pytest.importorskip("ouroboros.providers.litellm_adapter")
    LiteLLMAdapter = litellm_adapter.LiteLLMAdapter
    config = CompletionConfig(model="claude-sonnet-4-20250514", role="reflect")
    safe = LiteLLMAdapter(
        api_key="credential-a",
        api_base="https://provider.example/v1",
        max_retries=1,
    )
    unsafe = LiteLLMAdapter(
        api_key="credential-a",
        api_base="https://provider.example/v1",
        max_retries=2,
    )

    prepared_result = safe.frugality_prepare_completion(config)
    assert prepared_result.is_ok
    prepared = prepared_result.value
    normalized = provider_usage_module._completion_adapter_configuration(
        safe,
        prepared.contract,
    )
    assert normalized is not provider_usage_module._INVALID_CONFIGURATION
    assert "credential-a" not in repr(normalized)

    prepared_result = unsafe.frugality_prepare_completion(config)
    assert prepared_result.is_ok
    prepared = prepared_result.value
    assert (
        provider_usage_module._completion_adapter_configuration(
            unsafe,
            prepared.contract,
        )
        is provider_usage_module._INVALID_CONFIGURATION
    )


@pytest.mark.parametrize(
    ("setting", "value"),
    [
        ("dependency_client_sealed", False),
        ("dependency_client_type", "custom.Client"),
        ("dependency_transport_contract", "custom-transport"),
        ("dependency_cleanup_required", False),
        ("dependency_isolation_kind", "in-process"),
        ("dependency_worker_protocol", "custom-worker"),
        ("dependency_worker_environment_contract", "inherited-env"),
        ("dependency_worker_cwd_contract", "inherited-cwd"),
        ("dependency_mutation_audit", "polling"),
        ("worker_launcher_path", None),
        ("worker_executable_resolved_path", None),
        ("worker_executable_content_sha256", None),
        ("worker_python_version", None),
        ("worker_module", "custom.worker"),
        ("worker_implementation_sha256", None),
        ("adapter_module", "custom.adapter"),
        ("adapter_implementation_sha256", None),
        ("ouroboros_implementation_contract", "partial-sources"),
        ("ouroboros_implementation_sha256", None),
        ("httpx_dependency_version", None),
        ("openai_dependency_version", None),
    ],
)
def test_litellm_sealed_client_contract_fails_closed(
    setting: str,
    value: object,
) -> None:
    litellm_adapter = pytest.importorskip("ouroboros.providers.litellm_adapter")
    adapter = litellm_adapter.LiteLLMAdapter(
        api_key="credential-a",
        api_base="https://provider.example/v1",
        max_retries=1,
    )
    prepared_result = adapter.frugality_prepare_completion(
        CompletionConfig(model="openai/gpt-test", role="reflect")
    )

    assert prepared_result.is_ok
    contract = dict(prepared_result.value.contract)
    settings = dict(contract["settings"])
    settings[setting] = value
    contract["settings"] = settings
    assert (
        provider_usage_module._completion_adapter_configuration(adapter, contract)
        is provider_usage_module._INVALID_CONFIGURATION
    )


def test_litellm_worker_executable_authority_changes_configuration(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    litellm_adapter = pytest.importorskip("ouroboros.providers.litellm_adapter")
    adapter = litellm_adapter.LiteLLMAdapter(
        api_key="credential-a",
        api_base="https://provider.example/v1",
        max_retries=1,
    )
    config = CompletionConfig(model="openai/gpt-test", role="reflect")
    original = adapter.frugality_prepare_completion(config)
    alternate_executable = tmp_path / "python-worker"
    shutil.copy2(Path(litellm_adapter.sys.executable).resolve(strict=True), alternate_executable)
    alternate_executable.chmod(0o700)
    monkeypatch.setattr(litellm_adapter.sys, "executable", str(alternate_executable))
    changed = adapter.frugality_prepare_completion(config)

    assert original.is_ok
    assert changed.is_ok
    original_configuration = provider_usage_module._completion_adapter_configuration(
        adapter,
        original.value.contract,
    )
    changed_configuration = provider_usage_module._completion_adapter_configuration(
        adapter,
        changed.value.contract,
    )
    assert original_configuration is not provider_usage_module._INVALID_CONFIGURATION
    assert changed_configuration is not provider_usage_module._INVALID_CONFIGURATION
    assert original_configuration != changed_configuration
    original_settings = original.value.contract["settings"]
    changed_settings = changed.value.contract["settings"]
    assert isinstance(original_settings, Mapping)
    assert isinstance(changed_settings, Mapping)
    assert original_settings["worker_launcher_path"] != changed_settings["worker_launcher_path"]
    assert (
        original_settings["worker_executable_content_sha256"]
        == changed_settings["worker_executable_content_sha256"]
    )


def test_litellm_adapter_implementation_drift_invalidates_prepared_dispatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    litellm_adapter = pytest.importorskip("ouroboros.providers.litellm_adapter")
    adapter = litellm_adapter.LiteLLMAdapter(
        api_key="credential-a",
        api_base="https://provider.example/v1",
        max_retries=1,
    )
    prepared_result = adapter.frugality_prepare_completion(
        CompletionConfig(model="openai/gpt-test", role="reflect")
    )
    assert prepared_result.is_ok
    original_module_digest = litellm_adapter._module_implementation_sha256

    def drifted_module_digest(module_name: str) -> str:
        if module_name == "ouroboros.providers.litellm_adapter":
            return "f" * 64
        return original_module_digest(module_name)

    monkeypatch.setattr(
        litellm_adapter,
        "_module_implementation_sha256",
        drifted_module_digest,
    )

    assert adapter.frugality_validate_prepared_completion(prepared_result.value) is False


@pytest.mark.asyncio
async def test_litellm_adapter_implementation_drift_makes_usage_incomplete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    litellm_adapter = pytest.importorskip("ouroboros.providers.litellm_adapter")
    adapter = litellm_adapter.LiteLLMAdapter(
        api_key="credential-a",
        api_base="https://provider.example/v1",
        frugality_proof=True,
    )
    original_prepare = adapter.frugality_prepare_completion
    original_module_digest = litellm_adapter._module_implementation_sha256

    def prepare_then_drift(config: CompletionConfig):  # noqa: ANN202
        prepared_result = original_prepare(config)

        def drifted_module_digest(module_name: str) -> str:
            if module_name == "ouroboros.providers.litellm_adapter":
                return "f" * 64
            return original_module_digest(module_name)

        monkeypatch.setattr(
            litellm_adapter,
            "_module_implementation_sha256",
            drifted_module_digest,
        )
        return prepared_result

    async def complete_prepared(
        messages: object,
        prepared: PreparedFrugalityCompletion,
    ) -> Result[CompletionResponse, ProviderError]:
        del messages
        return Result.ok(
            CompletionResponse(
                content="done",
                model=prepared.config.model,
                usage=UsageInfo(prompt_tokens=3, completion_tokens=2, total_tokens=5),
            )
        )

    monkeypatch.setattr(adapter, "frugality_prepare_completion", prepare_then_drift)
    monkeypatch.setattr(adapter, "frugality_complete_prepared", complete_prepared)

    with capture_generation_provider_usage() as capture:
        result = await tracked_complete(
            adapter,
            [],
            CompletionConfig(model="openai/gpt-test", role="reflect"),
        )

    assert result.is_ok
    summary = capture.summary()
    assert summary.call_count == 1
    assert summary.token_spend is None
    assert summary.complete is False


def test_litellm_global_or_implicit_endpoint_authority_is_ineligible(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    litellm_adapter = pytest.importorskip("ouroboros.providers.litellm_adapter")
    LiteLLMAdapter = litellm_adapter.LiteLLMAdapter
    monkeypatch.setattr(litellm_adapter.litellm, "api_base", "https://global.example/v1")
    adapter = LiteLLMAdapter(api_key="credential-a", api_base=None, max_retries=1)

    prepared_result = adapter.frugality_prepare_completion(
        CompletionConfig(model="openai/gpt-4", role="reflect")
    )

    assert prepared_result.is_ok
    assert (
        provider_usage_module._completion_adapter_configuration(
            adapter,
            prepared_result.value.contract,
        )
        is provider_usage_module._INVALID_CONFIGURATION
    )


@pytest.mark.parametrize(
    ("model", "expected_api_base", "expected_client_type"),
    [
        (
            "anthropic/claude-test",
            "https://api.anthropic.com",
            "litellm.AsyncHTTPHandler",
        ),
        (
            "google/gemini-test",
            "https://generativelanguage.googleapis.com",
            "litellm.AsyncHTTPHandler",
        ),
        ("openai/gpt-test", "https://api.openai.com/v1", "openai.AsyncOpenAI"),
        (
            "openrouter/openai/gpt-test",
            "https://openrouter.ai/api/v1",
            "litellm.AsyncHTTPHandler",
        ),
    ],
)
def test_litellm_proof_adapter_seals_provider_default_endpoint(
    monkeypatch: pytest.MonkeyPatch,
    model: str,
    expected_api_base: str,
    expected_client_type: str,
) -> None:
    litellm_adapter = pytest.importorskip("ouroboros.providers.litellm_adapter")
    adapter = litellm_adapter.LiteLLMAdapter(
        api_key="credential-a",
        frugality_proof=True,
    )
    monkeypatch.setattr(litellm_adapter.litellm, "api_base", None)
    monkeypatch.setattr(adapter, "_load_credentials_config", lambda: None)
    for names in litellm_adapter._PROOF_API_BASE_ENVIRONMENT.values():
        for name in names:
            monkeypatch.delenv(name, raising=False)

    prepared_result = adapter.frugality_prepare_completion(
        CompletionConfig(model=model, role="reflect")
    )

    assert prepared_result.is_ok
    prepared = prepared_result.value
    settings = prepared.contract["settings"]
    assert isinstance(settings, Mapping)
    assert settings["api_base"] == expected_api_base
    assert settings["dependency_client_sealed"] is True
    assert settings["dependency_client_type"] == expected_client_type
    assert settings["dependency_transport_contract"] == "ouroboros.httpx.async.v1"
    assert settings["dependency_cleanup_required"] is True
    assert settings["dependency_isolation_kind"] == "subprocess"
    assert settings["dependency_worker_protocol"] == "ouroboros.litellm.worker.v1"
    assert settings["dependency_worker_environment_contract"] == "ouroboros.empty-env.v1"
    assert settings["dependency_worker_cwd_contract"] == "ouroboros.private-empty-tempdir.v1"
    assert settings["dependency_mutation_audit"] == "litellm.module-setattr.v1"
    assert Path(settings["worker_launcher_path"]).is_absolute()
    assert Path(settings["worker_executable_resolved_path"]).is_absolute()
    assert len(settings["worker_executable_content_sha256"]) == 64
    assert settings["worker_python_version"]
    assert settings["worker_module"] == "ouroboros.providers.litellm_proof_worker"
    assert len(settings["worker_implementation_sha256"]) == 64
    assert settings["adapter_module"] == "ouroboros.providers.litellm_adapter"
    assert len(settings["adapter_implementation_sha256"]) == 64
    assert settings["ouroboros_implementation_contract"] == "ouroboros.python-sources.v1"
    assert len(settings["ouroboros_implementation_sha256"]) == 64
    assert prepared.dispatch is not None
    assert prepared.dispatch.api_base == expected_api_base
    assert prepared.dispatch.client_type == expected_client_type
    assert (
        provider_usage_module._completion_adapter_configuration(
            adapter,
            prepared.contract,
        )
        is not provider_usage_module._INVALID_CONFIGURATION
    )


def test_litellm_proof_adapter_seals_environment_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    litellm_adapter = pytest.importorskip("ouroboros.providers.litellm_adapter")
    adapter = litellm_adapter.LiteLLMAdapter(
        api_key="credential-a",
        frugality_proof=True,
    )
    monkeypatch.setattr(litellm_adapter.litellm, "api_base", None)
    monkeypatch.setattr(adapter, "_load_credentials_config", lambda: None)
    monkeypatch.setenv("OPENAI_BASE_URL", " https://gateway.example/v1 ")

    prepared_result = adapter.frugality_prepare_completion(
        CompletionConfig(model="openai/gpt-test", role="reflect")
    )

    assert prepared_result.is_ok
    prepared = prepared_result.value
    settings = prepared.contract["settings"]
    assert isinstance(settings, Mapping)
    assert settings["api_base"] == "https://gateway.example/v1"
    assert prepared.dispatch is not None
    assert prepared.dispatch.api_base == "https://gateway.example/v1"


def test_litellm_credentialed_endpoint_is_proof_ineligible() -> None:
    litellm_adapter = pytest.importorskip("ouroboros.providers.litellm_adapter")
    adapter = litellm_adapter.LiteLLMAdapter(
        api_key="credential-a",
        api_base="https://user:guessable-password@provider.example/v1",
        frugality_proof=True,
    )

    prepared_result = adapter.frugality_prepare_completion(
        CompletionConfig(model="openai/gpt-test", role="reflect")
    )

    assert prepared_result.is_ok
    assert (
        provider_usage_module._completion_adapter_configuration(
            adapter,
            prepared_result.value.contract,
        )
        is provider_usage_module._INVALID_CONFIGURATION
    )


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("aclient_session", object()),
        ("organization", "org-drift"),
        ("headers", {"Authorization": "secret-header"}),
        ("api_version", "2026-08-04"),
        ("client_session", object()),
        ("modify_params", True),
        ("drop_params", True),
        ("network_mock", True),
        ("disable_aiohttp_transport", True),
        ("aiohttp_trust_env", True),
        ("disable_aiohttp_trust_env", True),
        ("force_ipv4", True),
        ("ssl_verify", False),
        ("ssl_security_level", "DEFAULT:@SECLEVEL=1"),
        ("ssl_certificate", "/tmp/client.pem"),
        ("ssl_ecdh_curve", "X25519"),
        ("use_aiohttp_transport", False),
    ],
)
def test_litellm_dependency_globals_are_proof_ineligible(
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    value: object,
) -> None:
    litellm_adapter = pytest.importorskip("ouroboros.providers.litellm_adapter")
    adapter = litellm_adapter.LiteLLMAdapter(
        api_key="credential-a",
        api_base="https://provider.example/v1",
        frugality_proof=True,
    )
    monkeypatch.setattr(litellm_adapter.litellm, name, value)

    prepared_result = adapter.frugality_prepare_completion(
        CompletionConfig(model="openai/gpt-test", role="reflect")
    )

    assert prepared_result.is_ok
    prepared = prepared_result.value
    if isinstance(value, str):
        assert value not in repr(prepared.contract)
    if isinstance(value, Mapping):
        assert "secret-header" not in repr(prepared.contract)
    assert (
        provider_usage_module._completion_adapter_configuration(
            adapter,
            prepared.contract,
        )
        is provider_usage_module._INVALID_CONFIGURATION
    )


@pytest.mark.parametrize(
    "name",
    [
        "OPENAI_ORGANIZATION",
        "OPENAI_PROJECT",
        "AIOHTTP_TRUST_ENV",
        "DISABLE_AIOHTTP_TRANSPORT",
        "DISABLE_AIOHTTP_TRUST_ENV",
        "EXPERIMENTAL_OPENAI_BASE_LLM_HTTP_HANDLER",
        "LITELLM_USER_AGENT",
        "SSL_CERTIFICATE",
        "SSL_ECDH_CURVE",
        "SSL_SECURITY_LEVEL",
        "SSL_VERIFY",
    ],
)
def test_litellm_unsealed_provider_environment_is_proof_ineligible(
    monkeypatch: pytest.MonkeyPatch,
    name: str,
) -> None:
    litellm_adapter = pytest.importorskip("ouroboros.providers.litellm_adapter")
    adapter = litellm_adapter.LiteLLMAdapter(
        api_key="credential-a",
        api_base="https://provider.example/v1",
        frugality_proof=True,
    )
    monkeypatch.setenv(name, "environment-drift")

    prepared_result = adapter.frugality_prepare_completion(
        CompletionConfig(model="openai/gpt-test", role="reflect")
    )

    assert prepared_result.is_ok
    assert (
        provider_usage_module._completion_adapter_configuration(
            adapter,
            prepared_result.value.contract,
        )
        is provider_usage_module._INVALID_CONFIGURATION
    )


def test_litellm_provider_config_override_is_proof_ineligible(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    litellm_adapter = pytest.importorskip("ouroboros.providers.litellm_adapter")
    adapter = litellm_adapter.LiteLLMAdapter(
        api_key="credential-a",
        api_base="https://provider.example/v1",
        frugality_proof=True,
    )
    monkeypatch.setattr(
        litellm_adapter.litellm.OpenAIConfig,
        "get_config",
        lambda: {"frequency_penalty": 0.5},
    )

    prepared_result = adapter.frugality_prepare_completion(
        CompletionConfig(model="openai/gpt-test", role="reflect")
    )

    assert prepared_result.is_ok
    assert (
        provider_usage_module._completion_adapter_configuration(
            adapter,
            prepared_result.value.contract,
        )
        is provider_usage_module._INVALID_CONFIGURATION
    )


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("aclient_session", object()),
        ("network_mock", True),
        ("disable_aiohttp_transport", True),
        ("aiohttp_trust_env", True),
        ("ssl_verify", False),
    ],
)
@pytest.mark.asyncio
async def test_litellm_dependency_drift_after_preparation_only_invalidates_proof(
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    value: object,
) -> None:
    litellm_adapter = pytest.importorskip("ouroboros.providers.litellm_adapter")
    adapter = litellm_adapter.LiteLLMAdapter(
        api_key="credential-a",
        api_base="https://provider.example/v1",
        frugality_proof=True,
    )
    original_prepare = adapter.frugality_prepare_completion

    def prepare_then_drift(config: CompletionConfig):  # noqa: ANN202
        prepared_result = original_prepare(config)
        monkeypatch.setattr(litellm_adapter.litellm, name, value)
        return prepared_result

    async def complete_prepared(
        messages: object,
        prepared: PreparedFrugalityCompletion,
    ) -> Result[CompletionResponse, ProviderError]:
        del messages
        return Result.ok(
            CompletionResponse(
                content="done",
                model=prepared.config.model,
                usage=UsageInfo(prompt_tokens=3, completion_tokens=2, total_tokens=5),
            )
        )

    monkeypatch.setattr(adapter, "frugality_prepare_completion", prepare_then_drift)
    monkeypatch.setattr(adapter, "frugality_complete_prepared", complete_prepared)

    with capture_generation_provider_usage() as capture:
        result = await tracked_complete(
            adapter,
            [],
            CompletionConfig(model="openai/gpt-test", role="reflect"),
        )

    assert result.is_ok
    summary = capture.summary()
    assert summary.call_count == 1
    assert summary.token_spend is None
    assert summary.complete is False


@pytest.mark.asyncio
async def test_litellm_inflight_mutate_and_restore_invalidates_proof(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    litellm_adapter = pytest.importorskip("ouroboros.providers.litellm_adapter")
    adapter = litellm_adapter.LiteLLMAdapter(
        api_key="credential-a",
        api_base="https://provider.example/v1",
        frugality_proof=True,
    )
    entered_dispatch = asyncio.Event()
    release_dispatch = asyncio.Event()

    async def isolated_completion(
        messages: object,
        config: CompletionConfig,
        dispatch: object,
    ) -> Result[CompletionResponse, ProviderError]:
        del messages, dispatch
        entered_dispatch.set()
        await release_dispatch.wait()
        return Result.ok(
            CompletionResponse(
                content="done",
                model=config.model,
                usage=UsageInfo(prompt_tokens=3, completion_tokens=2, total_tokens=5),
            )
        )

    async def mutate_and_restore() -> None:
        await entered_dispatch.wait()
        litellm_adapter.litellm.modify_params = True
        litellm_adapter.litellm.modify_params = False
        release_dispatch.set()

    mutation = asyncio.create_task(mutate_and_restore())
    monkeypatch.setattr(adapter, "_run_isolated_completion", isolated_completion)

    with capture_generation_provider_usage() as capture:
        result = await tracked_complete(
            adapter,
            [],
            CompletionConfig(model="openai/gpt-test", role="reflect"),
        )
    await mutation

    assert result.is_ok
    summary = capture.summary()
    assert summary.call_count == 1
    assert summary.token_spend is None
    assert summary.complete is False


@pytest.mark.asyncio
async def test_litellm_sealed_client_cleanup_failure_only_invalidates_proof(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    litellm_adapter = pytest.importorskip("ouroboros.providers.litellm_adapter")
    adapter = litellm_adapter.LiteLLMAdapter(
        api_key="credential-a",
        api_base="https://provider.example/v1",
        frugality_proof=True,
    )

    async def cleanup_failure(
        messages: object,
        config: CompletionConfig,
        dispatch: object,
    ) -> Result[CompletionResponse, ProviderError]:
        del messages
        dispatch.state.cleanup_failed = True
        return Result.ok(
            CompletionResponse(
                content="done",
                model=config.model,
                usage=UsageInfo(prompt_tokens=3, completion_tokens=2, total_tokens=5),
            )
        )

    monkeypatch.setattr(adapter, "_run_isolated_completion", cleanup_failure)

    with capture_generation_provider_usage() as capture:
        result = await tracked_complete(
            adapter,
            [],
            CompletionConfig(model="openai/gpt-test", role="reflect"),
        )

    assert result.is_ok
    summary = capture.summary()
    assert summary.call_count == 1
    assert summary.token_spend is None
    assert summary.complete is False


@pytest.mark.asyncio
async def test_litellm_tracked_dispatch_uses_attested_route_without_reresolving() -> None:
    litellm_adapter = pytest.importorskip("ouroboros.providers.litellm_adapter")
    adapter = litellm_adapter.LiteLLMAdapter(max_retries=1)
    adapter._get_api_key = MagicMock(side_effect=["credential-before", "credential-after"])
    adapter._get_api_base = MagicMock(
        side_effect=["https://before.example/v1", "https://after.example/v1"]
    )
    original_prepare = adapter.frugality_prepare_completion

    def _prepare_then_mutate(config: CompletionConfig):  # noqa: ANN202
        prepared = original_prepare(config)
        adapter._timeout = 999
        adapter._max_retries = 2
        return prepared

    adapter.frugality_prepare_completion = _prepare_then_mutate
    observed: dict[str, object] = {}

    async def isolated_completion(
        messages: object,
        config: CompletionConfig,
        dispatch: object,
    ) -> Result[CompletionResponse, ProviderError]:
        del messages
        observed.update(
            api_key=dispatch.api_key,
            api_base=dispatch.api_base,
            timeout=dispatch.timeout,
            max_retries=dispatch.max_retries,
            client_type=dispatch.client_type,
        )
        return Result.ok(
            CompletionResponse(
                content="done",
                model=config.model,
                usage=UsageInfo(prompt_tokens=4, completion_tokens=2, total_tokens=6),
            )
        )

    adapter._run_isolated_completion = isolated_completion
    with capture_generation_provider_usage() as capture:
        result = await tracked_complete(
            adapter,
            [],
            CompletionConfig(model="openai/gpt-4", role="reflect"),
        )

    assert result.is_ok
    assert capture.summary().complete
    adapter._get_api_key.assert_called_once_with("openai/gpt-4")
    adapter._get_api_base.assert_called_once_with("openai/gpt-4")
    assert observed == {
        "api_key": "credential-before",
        "api_base": "https://before.example/v1",
        "timeout": 60.0,
        "max_retries": 1,
        "client_type": "openai.AsyncOpenAI",
    }


@pytest.mark.asyncio
async def test_litellm_isolated_worker_uses_empty_environment_and_stdin_authority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    litellm_adapter = pytest.importorskip("ouroboros.providers.litellm_adapter")
    adapter = litellm_adapter.LiteLLMAdapter(
        api_key="credential-a",
        api_base="https://provider.example/v1",
        frugality_proof=True,
    )
    prepared_result = adapter.frugality_prepare_completion(
        CompletionConfig(model="openai/gpt-test", role="reflect")
    )
    assert prepared_result.is_ok
    dispatch = prepared_result.value.dispatch
    assert dispatch is not None
    observed: dict[str, object] = {}
    worker_report = {
        "ok": True,
        "proof": {
            "authority_verified": True,
            "client_cleanup_succeeded": True,
            "client_type": "openai.AsyncOpenAI",
            "httpx_dependency_version": litellm_adapter._HTTPX_DEPENDENCY_VERSION,
            "litellm_dependency_version": litellm_adapter._LITELLM_DEPENDENCY_VERSION,
            "openai_dependency_version": litellm_adapter._OPENAI_DEPENDENCY_VERSION,
            "transport_contract": litellm_adapter._PROOF_TRANSPORT_CONTRACT,
            "worker_executable_resolved_path": (dispatch.worker_authority.resolved_executable_path),
            "worker_executable_content_sha256": (
                dispatch.worker_authority.executable_content_sha256
            ),
            "worker_python_version": dispatch.worker_authority.python_version,
            "worker_module": dispatch.worker_authority.worker_module,
            "worker_implementation_sha256": (
                dispatch.worker_authority.worker_implementation_sha256
            ),
            "adapter_module": dispatch.worker_authority.adapter_module,
            "adapter_implementation_sha256": (
                dispatch.worker_authority.adapter_implementation_sha256
            ),
            "ouroboros_implementation_contract": (
                dispatch.worker_authority.ouroboros_implementation_contract
            ),
            "ouroboros_implementation_sha256": (
                dispatch.worker_authority.ouroboros_implementation_sha256
            ),
        },
        "response": {
            "content": "done",
            "finish_reason": "stop",
            "model": "openai/gpt-test",
            "usage": {
                "completion_tokens": 2,
                "prompt_tokens": 3,
                "total_tokens": 5,
            },
        },
    }

    class _Process:
        returncode = 0

        async def communicate(self, payload: bytes) -> tuple[bytes, bytes]:
            observed["payload"] = payload
            return json.dumps(worker_report).encode(), b""

    async def create_process(*command: str, **kwargs: object) -> _Process:
        observed["command"] = command
        observed["environment"] = kwargs["env"]
        return _Process()

    monkeypatch.setattr(litellm_adapter.asyncio, "create_subprocess_exec", create_process)

    result = await adapter._run_isolated_completion(
        [],
        prepared_result.value.config,
        dispatch,
    )

    assert result.is_ok
    command = observed["command"]
    assert isinstance(command, tuple)
    assert command[1:] == (
        "-I",
        "-m",
        "ouroboros.providers.litellm_proof_worker",
    )
    assert observed["environment"] == {
        "PYTHONIOENCODING": "utf-8",
        "PYTHONUTF8": "1",
    }
    assert "credential-a" not in repr(command)
    assert "credential-a" not in repr(observed["environment"])
    payload = json.loads(observed["payload"])
    assert payload["completion_kwargs"]["api_key"] == "credential-a"


async def test_provider_configuration_assignments_reject_role_swaps() -> None:
    with capture_generation_provider_usage() as first:
        await tracked_complete(
            _Adapter(20),
            [],
            CompletionConfig(model="model-a", role="wonder"),
        )
        await tracked_complete(
            _Adapter(20),
            [],
            CompletionConfig(model="model-b", role="semantic_evaluation"),
        )
    with capture_generation_provider_usage() as second:
        await tracked_complete(
            _Adapter(20),
            [],
            CompletionConfig(model="model-b", role="wonder"),
        )
        await tracked_complete(
            _Adapter(20),
            [],
            CompletionConfig(model="model-a", role="semantic_evaluation"),
        )

    first_summary = first.summary()
    second_summary = second.summary()
    assert first_summary.configuration_fingerprint != second_summary.configuration_fingerprint
    assert first_summary.configuration_assignments != second_summary.configuration_assignments


async def test_provider_configuration_assignments_preserve_call_multiplicity() -> None:
    with capture_generation_provider_usage() as first:
        for _ in range(9):
            await tracked_complete(
                _Adapter(100),
                [],
                CompletionConfig(model="expensive", role="reflect"),
            )
        await tracked_complete(
            _Adapter(1),
            [],
            CompletionConfig(model="cheap", role="reflect"),
        )
    with capture_generation_provider_usage() as second:
        await tracked_complete(
            _Adapter(100),
            [],
            CompletionConfig(model="expensive", role="reflect"),
        )
        for _ in range(9):
            await tracked_complete(
                _Adapter(1),
                [],
                CompletionConfig(model="cheap", role="reflect"),
            )

    first_summary = first.summary()
    second_summary = second.summary()
    assert first_summary.configuration_fingerprint == second_summary.configuration_fingerprint
    assert first_summary.configuration_assignments != second_summary.configuration_assignments


async def test_missing_or_unknown_provider_role_fails_closed() -> None:
    for role in (None, "", "unknown", "UNKNOWN", " Unknown "):
        with capture_generation_provider_usage() as capture:
            await tracked_complete(
                _Adapter(20),
                [],
                CompletionConfig(model="test-model", role=role),
            )

        summary = capture.summary()
        assert summary.complete is False
        assert summary.token_spend is None
        assert "instrumentation is incomplete" in "; ".join(summary.issues)


async def test_unknown_completion_backend_fails_closed() -> None:
    for backend in ("", "   ", "unknown", "UNKNOWN", " Unknown "):
        with capture_generation_provider_usage() as capture:
            await tracked_complete(
                _Adapter(20, llm_backend=backend),
                [],
                CompletionConfig(model="test-model", role="reflect"),
            )

        summary = capture.summary()
        assert summary.complete is False
        assert summary.token_spend is None
        assert "instrumentation is incomplete" in "; ".join(summary.issues)


async def test_unknown_completion_profile_or_effort_fails_closed() -> None:
    configs = (
        CompletionConfig(model="test-model", role="reflect", profile=" Unknown "),
        CompletionConfig(
            model="test-model",
            role="reflect",
            reasoning_effort=" Unknown ",  # type: ignore[arg-type]
        ),
    )
    for config in configs:
        with capture_generation_provider_usage() as capture:
            await tracked_complete(_Adapter(20), [], config)

        summary = capture.summary()
        assert summary.complete is False
        assert summary.token_spend is None
        assert "instrumentation is incomplete" in "; ".join(summary.issues)


async def test_unknown_provider_model_fails_closed_case_insensitively() -> None:
    for model in ("unknown", "UNKNOWN", " Unknown "):
        with capture_generation_provider_usage() as capture:
            await tracked_complete(
                _Adapter(20),
                [],
                CompletionConfig(model=model, role="reflect"),
            )

        summary = capture.summary()
        assert summary.complete is False
        assert summary.token_spend is None
        assert "instrumentation is incomplete" in "; ".join(summary.issues)


async def test_missing_or_invalid_resolved_provider_model_fails_closed() -> None:
    for response_model in (None, "", "   ", " Unknown "):
        with capture_generation_provider_usage() as capture:
            await tracked_complete(
                _Adapter(20, response_model=response_model),  # type: ignore[arg-type]
                [],
                CompletionConfig(model="requested-model", role="reflect"),
            )

        summary = capture.summary()
        assert summary.complete is False
        assert summary.token_spend is None
        assert "instrumentation is incomplete" in "; ".join(summary.issues)


async def test_generation_provider_usage_rejects_opaque_call_sites() -> None:
    with capture_generation_provider_usage() as capture:
        pass

    summary = capture.summary(instrumentation_complete=False)

    assert summary.complete is False
    assert summary.token_spend is None
    assert "instrumentation is incomplete" in "; ".join(summary.issues)


def test_bound_method_inherits_its_engine_tracking_contract() -> None:
    class _TrackedEngine:
        frugality_provider_tracking = True

        def phase(self) -> None:
            return None

    with capture_generation_provider_usage() as capture:
        observe_callable(_TrackedEngine().phase)

    assert capture.summary().complete is True


async def test_unknown_or_malformed_agent_runtime_attestation_fails_closed() -> None:
    class _UnknownRuntime(_AttestedRuntime):
        pass

    unknown = _UnknownRuntime()
    malformed = _AttestedRuntime()
    malformed.frugality_runtime_attestation = lambda: {  # type: ignore[method-assign]
        "schema_version": 1,
        "schema_id": "test.agent_runtime.v1",
        "implementation": f"{type(malformed).__module__}.{type(malformed).__qualname__}",
        "runtime_backend": "test-runtime",
        "runtime_handle_backend": "test-runtime-handle",
        "settings": {"api_key": "secret"},
    }

    for runtime in (unknown, malformed):
        with capture_generation_provider_usage() as capture:
            _ = [
                message
                async for message in tracked_agent_task(
                    runtime,
                    role="executor_auxiliary",
                    prompt="analyze",
                )
            ]
        assert capture.summary().complete is False
        assert capture.summary().token_spend is None


async def test_runtime_attestation_cleanup_failure_invalidates_only_proof() -> None:
    runtime = _AttestedRuntime()

    def fail_cleanup() -> None:
        raise RuntimeError("cleanup failed")

    runtime.frugality_runtime_attestation_complete = fail_cleanup  # type: ignore[attr-defined]
    with capture_generation_provider_usage() as capture:
        messages = [
            message
            async for message in tracked_agent_task(
                runtime,
                role="executor_auxiliary",
                prompt="analyze",
            )
        ]

    assert len(messages) == 1
    assert capture.summary().complete is False
    assert capture.summary().token_spend is None


async def test_codex_runtime_attestation_fingerprint_tracks_effect_bearing_drift(
    tmp_path: Path,
) -> None:
    cli = _write_fake_codex_cli(tmp_path / "codex-a", version="1.0")
    other_cli = _write_fake_codex_cli(tmp_path / "codex-b", version="2.0")
    skills_a = tmp_path / "skills-a"
    skills_b = tmp_path / "skills-b"
    skills_a.mkdir()
    skills_b.mkdir()

    class _StableDispatcher:
        def __init__(self, identity: str) -> None:
            self.identity = identity

        def stable_identity_contract(self) -> Mapping[str, object]:
            return {"schema_version": 1, "identity": self.identity}

        async def __call__(self, *args, **kwargs):  # noqa: ANN201, ARG002
            return ()

    baseline = CodexCliRuntime(cli_path=cli, model="test-model")
    variants = [
        CodexCliRuntime(cli_path=cli, model="test-model", runtime_profile="worker"),
        CodexCliRuntime(cli_path=other_cli, model="test-model"),
        CodexCliRuntime(
            cli_path=cli,
            model="test-model",
            startup_output_timeout_seconds=15,
        ),
        CodexCliRuntime(cli_path=cli, model="test-model", skills_dir=skills_a),
        CodexCliRuntime(cli_path=cli, model="test-model", skills_dir=skills_b),
        CodexCliRuntime(
            cli_path=cli,
            model="test-model",
            skill_dispatcher=_StableDispatcher("dispatcher-a"),
        ),
        CodexCliRuntime(
            cli_path=cli,
            model="test-model",
            skill_dispatcher=_StableDispatcher("dispatcher-b"),
        ),
    ]
    backend_specific = CodexCliRuntime(cli_path=cli, model="test-model")
    backend_specific._codex_config_fingerprint = "f" * 64
    variants.append(backend_specific)

    baseline_summary = await _codex_runtime_summary(baseline)
    variant_summaries = [await _codex_runtime_summary(runtime) for runtime in variants]

    assert baseline_summary.complete is True
    assert all(summary.complete for summary in variant_summaries)
    assert all(
        summary.configuration_fingerprint != baseline_summary.configuration_fingerprint
        for summary in variant_summaries
    )


async def test_codex_runtime_attestation_tracks_repository_relative_cwd(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cli = _write_fake_codex_cli(tmp_path / "codex", version="1.0")
    repository_root = tmp_path / "repository"
    (repository_root / "src").mkdir(parents=True)

    def project_identity(cwd: str) -> SimpleNamespace:
        relative = Path(cwd).relative_to(repository_root).as_posix() or "."
        return SimpleNamespace(workspace_path=relative)

    monkeypatch.setattr(
        frugality_attestation_module,
        "resolve_project_identity",
        project_identity,
    )
    root_runtime = CodexCliRuntime(
        cli_path=cli,
        model="test-model",
        cwd=repository_root,
    )
    nested_runtime = CodexCliRuntime(
        cli_path=cli,
        model="test-model",
        cwd=repository_root / "src",
    )

    root_contract = root_runtime.frugality_runtime_attestation()
    nested_contract = nested_runtime.frugality_runtime_attestation()
    root_summary = await _codex_runtime_summary(root_runtime)
    nested_summary = await _codex_runtime_summary(nested_runtime)

    assert root_contract["settings"]["semantic_cwd"] == "."  # type: ignore[index]
    assert nested_contract["settings"]["semantic_cwd"] == "src"  # type: ignore[index]
    assert root_summary.complete is True
    assert nested_summary.complete is True
    assert root_summary.configuration_fingerprint != nested_summary.configuration_fingerprint


@pytest.mark.parametrize(
    ("key", "baseline_value", "changed_value"),
    [
        ("OPENAI_BASE_URL", "https://provider-a.example/v1", "https://provider-b.example/v1"),
        ("OPENAI_API_KEY", "test-credential-a", "test-credential-b"),
        ("PATH", "/opt/toolchain-a/bin", "/opt/toolchain-b/bin"),
        ("CODEX_SANDBOX_NETWORK_DISABLED", "1", "0"),
    ],
)
async def test_codex_runtime_attestation_tracks_complete_child_environment_authority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    key: str,
    baseline_value: str,
    changed_value: str,
) -> None:
    cli = _write_fake_codex_cli(tmp_path / "codex", version="1.0")
    if key == "PATH":
        inherited_path = os.environ.get("PATH", "")
        baseline_value = os.pathsep.join((baseline_value, inherited_path))
        changed_value = os.pathsep.join((changed_value, inherited_path))

    with monkeypatch.context() as environment:
        environment.setenv(key, baseline_value)
        baseline_summary = await _codex_runtime_summary(
            CodexCliRuntime(cli_path=cli, model="test-model")
        )
    with monkeypatch.context() as environment:
        environment.setenv(key, changed_value)
        changed_summary = await _codex_runtime_summary(
            CodexCliRuntime(cli_path=cli, model="test-model")
        )

    assert baseline_summary.complete is True
    assert changed_summary.complete is True
    assert baseline_summary.configuration_fingerprint != changed_summary.configuration_fingerprint
    assert baseline_summary.configuration_assignments != changed_summary.configuration_assignments


def test_codex_runtime_attestation_never_persists_child_credentials(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "test-secret-that-must-not-escape"
    cli = _write_fake_codex_cli(tmp_path / "codex", version="1.0")
    monkeypatch.setenv("OPENAI_API_KEY", secret)
    runtime = CodexCliRuntime(cli_path=cli, model="test-model")

    contract = runtime.frugality_runtime_attestation()
    encoded = json.dumps(contract, sort_keys=True)

    assert contract["schema_version"] == 4
    assert contract["schema_id"] == "ouroboros.codex_cli_runtime.v4"
    assert secret not in encoded
    assert "hmac-sha256:" not in encoded


def test_installation_authority_key_is_owner_only_and_stable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(credential_authority_module, "get_config_dir", lambda: tmp_path)

    first = _real_installation_authority_key()
    second = _real_installation_authority_key()
    key_path = tmp_path / "frugality-authority.key"

    assert first is not None and len(first) == 32
    assert second == first
    assert key_path.stat().st_mode & 0o777 == 0o600
    key_path.chmod(0o644)
    assert _real_installation_authority_key() is None


def test_installation_authority_key_recovers_owner_only_partial_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(credential_authority_module, "get_config_dir", lambda: tmp_path)
    key_path = tmp_path / "frugality-authority.key"
    key_path.write_bytes(b"partial")
    key_path.chmod(0o600)

    recovered = _real_installation_authority_key()

    assert recovered is not None and len(recovered) == 32
    assert key_path.read_bytes() == recovered


def test_installation_authority_key_interrupted_write_is_replay_safe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_write = os.write
    writes = 0

    def interrupted_write(descriptor: int, value: object) -> int:
        nonlocal writes
        writes += 1
        if writes == 1:
            return real_write(descriptor, bytes(value)[:1])  # type: ignore[arg-type]
        raise OSError("simulated interrupted write")

    with monkeypatch.context() as interrupted:
        interrupted.setattr(credential_authority_module, "get_config_dir", lambda: tmp_path)
        interrupted.setattr(credential_authority_module.os, "write", interrupted_write)
        assert _real_installation_authority_key() is None

    key_path = tmp_path / "frugality-authority.key"
    assert not key_path.exists()
    assert not tuple(tmp_path.glob(".frugality-authority.key.*.tmp"))
    with monkeypatch.context() as recovered_context:
        recovered_context.setattr(
            credential_authority_module,
            "get_config_dir",
            lambda: tmp_path,
        )
        recovered = _real_installation_authority_key()
    assert recovered is not None and len(recovered) == 32


def test_codex_runtime_spawns_with_exact_environment_bound_by_attestation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cli = _write_fake_codex_cli(tmp_path / "codex", version="1.0")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://provider-a.example/v1")
    runtime = CodexCliRuntime(cli_path=cli, model="test-model")
    _ = runtime.frugality_runtime_attestation()

    monkeypatch.setenv("OPENAI_BASE_URL", "https://provider-b.example/v1")
    first_attested_environment = runtime._build_child_env()
    retry_attested_environment = runtime._build_child_env()
    runtime.frugality_runtime_attestation_complete()
    next_environment = runtime._build_child_env()

    assert first_attested_environment["OPENAI_BASE_URL"] == "https://provider-a.example/v1"
    assert retry_attested_environment["OPENAI_BASE_URL"] == "https://provider-a.example/v1"
    assert next_environment["OPENAI_BASE_URL"] == "https://provider-b.example/v1"


def test_codex_child_environment_attestation_is_bounded() -> None:
    oversized = {"PATH": "x" * (128 * 1_024 + 1)}

    assert codex_child_environment_attestation(oversized) is None


def test_persisted_environment_evidence_cannot_verify_low_entropy_secret_guesses() -> None:
    environment = {"PATH": "/usr/bin", "DATABASE_PASSWORD": "guessable-password"}

    unavailable = codex_child_environment_attestation(environment)
    first_installation = codex_child_environment_attestation(
        environment,
        secret_authority_key=b"a" * 32,
    )
    second_installation = codex_child_environment_attestation(
        environment,
        secret_authority_key=b"b" * 32,
    )

    assert unavailable is None
    assert first_installation is not None
    assert second_installation is not None
    assert (
        first_installation["authority_fingerprint"] != second_installation["authority_fingerprint"]
    )
    persisted = json.dumps(first_installation, sort_keys=True)
    assert "guessable-password" not in persisted
    assert "hmac-sha256:" not in persisted


@pytest.mark.parametrize("key", ["GH_TOKEN", "GITHUB_TOKEN", "NPM_TOKEN", "HF_TOKEN"])
def test_common_token_aliases_use_installation_keyed_authority(key: str) -> None:
    environment = {"PATH": "/usr/bin", key: "low-entropy-token"}

    first = codex_child_environment_attestation(
        environment,
        secret_authority_key=b"a" * 32,
    )
    second = codex_child_environment_attestation(
        environment,
        secret_authority_key=b"b" * 32,
    )

    assert first is not None and first["sensitive_entry_count"] == 1
    assert second is not None and second["sensitive_entry_count"] == 1
    assert first["authority_fingerprint"] != second["authority_fingerprint"]


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("DATABASE_URL", "postgres://user:guessable-password@db.example/app"),
        ("HTTPS_PROXY", "https://proxy-user:guessable-password@proxy.example:8443"),
    ],
)
def test_embedded_environment_credentials_are_installation_keyed(
    key: str,
    value: str,
) -> None:
    environment = {"PATH": "/usr/bin", key: value}

    first = codex_child_environment_attestation(
        environment,
        secret_authority_key=b"a" * 32,
    )
    second = codex_child_environment_attestation(
        environment,
        secret_authority_key=b"b" * 32,
    )

    assert first is not None
    assert second is not None
    assert first["authority_fingerprint"] != second["authority_fingerprint"]
    persisted = json.dumps(first, sort_keys=True)
    assert value not in persisted
    assert "guessable-password" not in persisted


def test_provider_credential_authority_is_installation_keyed() -> None:
    credential = "low-entropy-api-key"

    first = credential_authority_digest(credential, installation_key=b"a" * 32)
    second = credential_authority_digest(credential, installation_key=b"b" * 32)

    assert first is not None
    assert second is not None
    assert first != second
    assert credential not in first
    assert first.startswith("hmac-sha256-installation-key-v2:")
    assert credential_authority_digest(credential, installation_key=b"short") is None
    assert credential_authority_digest(f" {credential}", installation_key=b"a" * 32) != first


async def test_opaque_executor_invalidates_generation_provider_completeness() -> None:
    async def executor(seed: object, *, parallel: bool) -> object:
        return seed, parallel

    with capture_generation_provider_usage() as capture:
        await call_executor(
            executor,
            SimpleNamespace(),  # type: ignore[arg-type]
            parallel=True,
            execution_id=None,
        )

    assert capture.summary().complete is False


async def test_auxiliary_agent_stream_usage_is_included_in_generation_total() -> None:
    with capture_generation_provider_usage() as capture:
        messages = [
            message
            async for message in tracked_agent_task(
                _AttestedRuntime(),
                role="executor_auxiliary",
                prompt="analyze",
            )
        ]

    summary = capture.summary()
    assert len(messages) == 1
    assert summary.complete is True
    assert summary.call_count == 1
    assert summary.token_spend == 150
    assert summary.configuration_fingerprint is not None


async def test_agent_runtime_explicit_blank_configuration_fails_closed() -> None:
    for field in (
        "runtime_backend",
        "llm_backend",
        "permission_mode",
        "reasoning_effort",
        "service_tier",
    ):
        runtime = _AttestedRuntime(usage={"total_tokens": 10})
        setattr(runtime, field, "   ")
        with capture_generation_provider_usage() as capture:
            _ = [
                message
                async for message in tracked_agent_task(
                    runtime,
                    role="executor_auxiliary",
                    prompt="analyze",
                )
            ]

        summary = capture.summary()
        assert summary.complete is False, field
        assert summary.token_spend is None


async def test_auxiliary_agent_stream_usage_must_reconcile() -> None:
    with capture_generation_provider_usage() as capture:
        _ = [
            message
            async for message in tracked_agent_task(
                _AttestedRuntime(
                    usage={"input_tokens": 100, "output_tokens": 50, "total_tokens": 1}
                ),
                role="executor_auxiliary",
                prompt="analyze",
            )
        ]

    summary = capture.summary()
    assert summary.complete is False
    assert summary.token_spend is None


async def test_auxiliary_agent_stream_rejects_partial_fallback_subtotals() -> None:
    for usage in (
        {"input_tokens": 10},
        {"output_tokens": 5},
        {"cache_read_input_tokens": 8},
    ):
        with capture_generation_provider_usage() as capture:
            _ = [
                message
                async for message in tracked_agent_task(
                    _AttestedRuntime(usage=usage),
                    role="executor_auxiliary",
                    prompt="analyze",
                )
            ]

        assert capture.summary().complete is False


async def test_agent_resume_state_is_configuration_bound_or_fails_closed() -> None:
    async def _capture(handle: object):  # noqa: ANN202
        with capture_generation_provider_usage() as capture:
            _ = [
                message
                async for message in tracked_agent_task(
                    _AttestedRuntime(),
                    role="executor_auxiliary",
                    prompt="analyze",
                    resume_handle=handle,
                )
            ]
        return capture.summary()

    fresh = await _capture(None)
    scoped_fresh = await _capture(
        SimpleNamespace(
            backend="test-runtime",
            kind="agent_runtime",
            metadata={},
            native_session_id=None,
            conversation_id=None,
            previous_response_id=None,
            transcript_path=None,
            approval_mode="acceptEdits",
            cwd="/tmp/isolated",
        )
    )
    opaque = await _capture(object())
    resumed = await _capture(
        SimpleNamespace(
            backend="test-runtime",
            kind="agent_runtime",
            metadata={},
            native_session_id="session-1",
            conversation_id=None,
            previous_response_id=None,
            transcript_path=None,
            approval_mode="acceptEdits",
            cwd="/tmp/isolated",
        )
    )

    assert fresh.complete is True
    assert scoped_fresh.complete is True
    assert fresh.configuration_assignments != scoped_fresh.configuration_assignments
    assert opaque.complete is False
    assert resumed.complete is False

    metadata_opaque = await _capture(
        SimpleNamespace(
            backend="test-runtime",
            kind="agent_runtime",
            metadata={"control_plane": [{"action": "resume"}]},
            native_session_id=None,
            conversation_id=None,
            previous_response_id=None,
            transcript_path=None,
            approval_mode="acceptEdits",
            cwd="/tmp/isolated",
        )
    )
    assert metadata_opaque.complete is False

    with capture_generation_provider_usage() as unknown_setting_capture:
        _ = [
            message
            async for message in tracked_agent_task(
                _AttestedRuntime(),
                role="executor_auxiliary",
                prompt="analyze",
                model=" Unknown ",
            )
        ]
    assert unknown_setting_capture.summary().complete is False


async def test_agent_stream_binds_observed_effective_model() -> None:
    async def _capture(effective_model: object):  # noqa: ANN202
        with capture_generation_provider_usage() as capture:
            _ = [
                message
                async for message in tracked_agent_task(
                    _AttestedRuntime(
                        usage={"total_tokens": 100},
                        effective_model=effective_model,
                    ),
                    role="executor_auxiliary",
                    prompt="analyze",
                )
            ]
        return capture.summary()

    expensive = await _capture("expensive-model")
    cheap = await _capture("cheap-model")
    unknown = await _capture(" Unknown ")
    missing = await _capture(None)

    assert expensive.complete is True
    assert cheap.complete is True
    assert expensive.configuration_assignments != cheap.configuration_assignments
    assert unknown.complete is False
    assert missing.complete is False


async def test_agent_stream_without_effective_model_observation_fails_closed() -> None:
    with capture_generation_provider_usage() as capture:
        _ = [
            message
            async for message in tracked_agent_task(
                _AttestedRuntime(usage={"total_tokens": 100}, effective_model=None),
                role="executor_auxiliary",
                prompt="analyze",
            )
        ]

    assert capture.summary().complete is False


async def test_agent_result_without_effective_model_observation_fails_closed() -> None:
    with capture_generation_provider_usage() as capture:
        await tracked_agent_task_to_result(
            _AttestedRuntime(usage={"total_tokens": 100}, effective_model=None),
            role="validation_repair",
            prompt="repair",
        )

    assert capture.summary().complete is False


async def test_auxiliary_agent_stream_overflow_fails_closed() -> None:
    with capture_generation_provider_usage() as capture:
        messages = [
            message
            async for message in tracked_agent_task(
                _AttestedRuntime(usage={"total_tokens": 10**400}),
                role="executor_auxiliary",
                prompt="analyze",
            )
        ]

    summary = capture.summary()
    assert len(messages) == 1
    assert summary.complete is False
    assert summary.token_spend is None
    assert "missing or invalid" in "; ".join(summary.issues)
