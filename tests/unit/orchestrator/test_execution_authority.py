from __future__ import annotations

import asyncio
from functools import wraps
import gc
import hashlib
import os
import random
from types import MethodType, SimpleNamespace, coroutine
from unittest.mock import AsyncMock, MagicMock
import weakref

import pytest

from ouroboros import codex_permissions
from ouroboros.orchestrator import execution_authority as execution_authority_module
from ouroboros.orchestrator import parallel_executor as parallel_executor_module
from ouroboros.orchestrator import rate_limit as rate_limit_module
from ouroboros.orchestrator.ac_runtime_handle_manager import ACRuntimeHandleManager
from ouroboros.orchestrator.adapter import FULL_CAPABILITIES, RuntimeHandle
from ouroboros.orchestrator.claude_worker_runtime import ClaudeWorkerTransport
from ouroboros.orchestrator.codex_cli_runtime import CodexCliRuntime
from ouroboros.orchestrator.codex_mcp_runtime import CodexMcpWorkerTransport
from ouroboros.orchestrator.coordinator import LevelCoordinator
from ouroboros.orchestrator.evidence import verification as transcript_verification
from ouroboros.orchestrator.execution_authority import (
    ExecutionAuthorityContract,
    ResolvedRuntimeAuthority,
    build_execution_policy_contract,
    canonical_workspace_authority,
    execution_authority_boundary_contract,
    runtime_authority_contract,
    runtime_execution_identity_contract,
    valid_constructor_model_contract,
    valid_runtime_execution_identity_contract,
)
from ouroboros.orchestrator.execution_event_emitter import ExecutionEventEmitter
from ouroboros.orchestrator.model_routing import ModelRouter, serialize_model_router
from ouroboros.orchestrator.parallel_executor import ParallelACExecutor
from ouroboros.orchestrator.pi_runtime import PiRuntime
from ouroboros.orchestrator.profile_loader import ExecutionProfile, load_profile
from ouroboros.orchestrator.rate_limit import (
    RateLimitGate,
    ResolvedDispatchRatePolicy,
    SharedRateLimitBucket,
    build_rate_limit_gate,
)
from ouroboros.orchestrator.synapse import SessionSignalHub
from ouroboros.orchestrator.verifier import VerifierVerdict
from ouroboros.orchestrator.worker_runtime import LeaderDrivenWorkerRuntime

_RATE_GATE_FACTORY_TEST_GLOBALS: dict[str, int] = {"limit": 9}


class _Runtime:
    capabilities = FULL_CAPABILITIES
    runtime_backend = "test-runtime"
    llm_backend = "test-llm"
    permission_mode = "bypassPermissions"
    working_directory: str | None = None
    _model = None

    def __init__(
        self,
        *,
        profile: str = "profile-a",
        cli_path: str | None = None,
        startup_timeout: float = 1.0,
        idle_timeout: float = 2.0,
    ) -> None:
        self.profile = profile
        self._cli_path = cli_path
        self._startup_output_timeout_seconds = startup_timeout
        self._stdout_idle_timeout_seconds = idle_timeout
        self._process_shutdown_timeout_seconds = 3.0
        self._completed_process_group_shutdown_timeout_seconds = 0.2

    def execution_identity_contract(self) -> dict[str, object]:
        return {
            "profile": self.profile,
            "effective_model_observed": True,
        }

    async def execute_task(self, **_: object):
        if False:  # pragma: no cover - implementation identity only
            yield None

    def resume_handle_execution_identity_contract(
        self,
        runtime_handle: RuntimeHandle | None,
    ) -> dict[str, object]:
        return {
            "profile": (
                runtime_handle.metadata.get("profile") if runtime_handle is not None else None
            )
        }


class _SensitiveWatchdogRuntime(_Runtime):
    def __init__(self, secret: str = "sk-sentinel-secret") -> None:
        super().__init__()
        self._secret = secret

    def watchdog_identity_contract(self) -> dict[str, object]:
        return {"connection_label": self._secret}


class _Verifier:
    def __init__(self, identity: str) -> None:
        self.identity = identity

    def verification_identity_contract(self) -> dict[str, object]:
        return {"judge": self.identity}

    def __call__(self, **_: object) -> VerifierVerdict:
        return VerifierVerdict(passed=True)


class _ExecutionOwner:
    def execute(self) -> None:
        """Stable test-only effect-owner implementation."""


class _HelperVerifierBase:
    def verification_identity_contract(self) -> dict[str, object]:
        return {"judge": "shared"}

    def __call__(self, **_: object) -> VerifierVerdict:
        return VerifierVerdict(passed=self._passed())

    def _passed(self) -> bool:
        raise NotImplementedError


class _PassingHelperVerifier(_HelperVerifierBase):
    def _passed(self) -> bool:
        return True


class _FailingHelperVerifier(_HelperVerifierBase):
    def _passed(self) -> bool:
        return False


class _SelfReferentialVerifier:
    def __init__(self) -> None:
        self.helper = self

    def verification_identity_contract(self) -> dict[str, object]:
        return {"judge": "self-referential"}

    def __call__(self, **_: object) -> VerifierVerdict:
        return VerifierVerdict(passed=True)


class _BypassDispatchRateExecutor(ParallelACExecutor):
    async def _await_dispatch_rate_budget(
        self,
        *,
        prompt: str,
        system_prompt: str | None,
    ) -> None:
        del prompt, system_prompt


class _ReplacementLeafDispatcher:
    """Distinct dispatch implementation for authority collision coverage."""

    def __init__(self, executor: ParallelACExecutor) -> None:
        self.executor = executor

    async def stream(self, **_: object) -> None:
        return None


class _StableDispatcher:
    def execution_identity_contract(self) -> dict[str, object]:
        return {"dispatcher": "stable"}

    async def __call__(self, **_: object) -> None:
        return None


class _SlottedRuntime:
    __slots__ = ("handler",)

    capabilities = FULL_CAPABILITIES
    runtime_backend = "test-runtime"
    llm_backend = "test-llm"
    permission_mode = "bypassPermissions"
    working_directory = None
    _model = None

    def __init__(self, handler: object) -> None:
        self.handler = handler

    def execution_identity_contract(self) -> dict[str, object]:
        return {"effective_model_observed": True, "profile": "slotted"}

    async def execute_task(self, **_: object):
        if False:  # pragma: no cover - implementation identity only
            yield self.handler


def _generation(label: str = "generation-a") -> dict[str, object]:
    return {
        "version": 1,
        "kind": "test-snapshot-v1",
        "digest": "sha256:" + hashlib.sha256(label.encode()).hexdigest(),
    }


def _policy(*, backend: str = "test-runtime", **overrides: object) -> dict[str, object]:
    policy = build_execution_policy_contract(
        decomposition_mode="preflight",
        max_decomposition_depth=3,
        max_concurrent=2,
        execution_profile=None,
        fat_harness_mode=False,
        run_verify_commands=True,
        verify_command_timeout_seconds=600,
        ac_retry_attempts=2,
        reasoning_effort=None,
        model_router=None,
        cross_harness_redispatch=False,
        shadow_replay_enabled=False,
        dispatch_rate_policy=ResolvedDispatchRatePolicy.resolve(
            backend=backend,
            self_governs_rate_limit=False,
            requests_per_minute=None,
            tokens_per_minute=None,
        ).to_contract_data(),
    )
    policy.update(overrides)
    return policy


def _contract(
    *,
    runtime: _Runtime | None = None,
    verifier: object | None = None,
    workspace: str = "/tmp/workspace-a",
    policy: dict[str, object] | None = None,
    generation: dict[str, object] | None = None,
    runtime_handle: RuntimeHandle | None = None,
) -> ExecutionAuthorityContract:
    return ExecutionAuthorityContract.build(
        adapter=runtime or _Runtime(),
        verifier=verifier,  # type: ignore[arg-type]
        executor=_ExecutionOwner(),
        workspace=workspace,
        execution_policy=policy if policy is not None else _policy(),
        workspace_generation=generation if generation is not None else _generation(),
        runtime_handle=runtime_handle,
    )


def test_authority_policy_redacts_credentials_without_mutating_live_policy() -> None:
    """Authority JSON may distinguish secrets, but must never serialize them."""
    first_secret = "ghp_" + "a" * 36
    second_secret = "ghp_" + "b" * 36

    def contract_for(secret: str) -> tuple[ExecutionAuthorityContract, dict[str, object]]:
        policy = _policy(reasoning_effort=secret)
        policy["model_routing"] = serialize_model_router(
            ModelRouter(
                tier_models={"frugal": secret},
                runtime_backend="claude",
                child_tier="frugal",
                base_tier="standard",
                escalation_retry_threshold=1,
            )
        )
        return _contract(policy=policy), policy

    first, first_live_policy = contract_for(first_secret)
    second, _ = contract_for(second_secret)

    assert first_live_policy["reasoning_effort"] == first_secret
    assert first_secret not in first.canonical_json
    assert second_secret not in second.canonical_json
    authority_policy = first.data["execution_policy"]
    assert authority_policy["reasoning_effort"].startswith("redacted:sha256:")
    router = authority_policy["model_routing"]["router"]
    assert router["tier_models"]["frugal"].startswith("redacted:sha256:")
    assert first.fingerprint != second.fingerprint


@pytest.mark.parametrize(
    "prefix",
    (" \t", "\x00", "\u200b", "\ufeff", "\u2060", "\u034f", "\ufe0f"),
)
def test_runtime_authority_marks_prefixed_credentials_unobserved(prefix: str) -> None:
    """Normalization must not serialize a credential with a hidden prefix."""
    runtime = _Runtime()
    runtime_secret = "ghp_" + "a" * 36
    llm_secret = "ghp_" + "b" * 36
    runtime.runtime_backend = f"{prefix}{runtime_secret}"
    runtime.llm_backend = f"{prefix}{llm_secret}"

    authority = runtime_authority_contract(runtime)

    assert authority["runtime_backend"] is None
    assert authority["runtime_backend_unobserved"] is True
    assert authority["llm_backend"] is None
    assert authority["llm_backend_unobserved"] is True
    assert runtime_secret not in str(authority)
    assert llm_secret not in str(authority)


def test_runtime_identity_validators_reject_nested_credentials() -> None:
    secret = "ghp_" + "a" * 36

    assert valid_constructor_model_contract({"observed": True, "model": secret}) is False
    assert (
        valid_runtime_execution_identity_contract(
            {
                "version": 1,
                "observed": True,
                "identity": {"effective_model_observed": True, "opaque": secret},
            }
        )
        is False
    )


def test_runtime_authority_binds_nested_command_helper_globals(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    """A nested permission-table mutation changes runtime authority."""
    runtime = CodexCliRuntime(
        cli_path="/usr/bin/true",
        cwd=str(tmp_path),
        permission_mode="default",
    )
    baseline = runtime_authority_contract(runtime)
    monkeypatch.setitem(
        codex_permissions._SANDBOX_TO_CODEX_ARGS,
        codex_permissions.SandboxClass.READ_ONLY,
        ["--sentinel-permission-flag"],
    )

    changed = runtime_authority_contract(runtime)

    assert baseline != changed
    assert changed["implementation"]["stability"] == "durable"
    assert runtime._build_permission_args() == ["--sentinel-permission-flag"]


def _worker_contract(
    transport: object,
    *,
    backend: str,
    llm_backend: str,
) -> ExecutionAuthorityContract:
    runtime = LeaderDrivenWorkerRuntime(
        transport=transport,  # type: ignore[arg-type]
        runtime_backend=backend,
        llm_backend=llm_backend,
    )
    return ExecutionAuthorityContract.build(
        adapter=runtime,
        verifier=None,
        executor=_ExecutionOwner(),
        workspace="/tmp/workspace-a",
        workspace_generation=_generation(),
        execution_policy=_policy(backend=backend),
    )


def test_runtime_profile_drift_changes_authority() -> None:
    assert (
        _contract(runtime=_Runtime(profile="a")).fingerprint
        != _contract(runtime=_Runtime(profile="b")).fingerprint
    )


def test_foundation_a_boundary_is_explicit_and_versioned() -> None:
    contract = _contract()

    assert contract.data["boundary"] == execution_authority_boundary_contract()
    assert "leaf_dispatcher_implementation" in contract.data["boundary"]["portable_baseline"]
    assert "ac_runtime_handle_manager" in contract.data["boundary"]["per_attempt_capsule"]
    assert "selected_runtime_handle" in contract.data["boundary"]["per_attempt_capsule"]
    assert "execution_event_emitter" in contract.data["boundary"]["volatile"]
    assert "session_signal_hub" in contract.data["boundary"]["volatile"]


def test_declared_live_component_cannot_upgrade_into_portable_baseline() -> None:
    class DeclaredSignalHub(SessionSignalHub):
        def execution_identity_contract(self) -> dict[str, object]:
            return {"version": 1, "configuration": {"protocol": "test"}}

    first = ExecutionAuthorityContract.build(
        adapter=_Runtime(),
        verifier=None,
        executor=_ExecutionOwner(),
        executor_components={"session_signal_hub": DeclaredSignalHub()},
        workspace="/tmp/workspace-a",
        workspace_generation=_generation(),
        execution_policy=_policy(),
    )
    second = ExecutionAuthorityContract.build(
        adapter=_Runtime(),
        verifier=None,
        executor=_ExecutionOwner(),
        executor_components={"session_signal_hub": DeclaredSignalHub()},
        workspace="/tmp/workspace-a",
        workspace_generation=_generation(),
        execution_policy=_policy(),
    )

    component = first.data["executor"]["components"]["session_signal_hub"]
    assert component["mode"] == "out_of_boundary"
    assert component["boundary"] == "volatile"
    assert component["stability"] == "process_local"
    assert first.portable_across_processes is False
    assert first.fingerprint != second.fingerprint


def test_runtime_executable_and_watchdog_drift_change_authority() -> None:
    baseline = _contract(runtime=_Runtime(cli_path="/bin/true"))
    changed_executable = _contract(runtime=_Runtime(cli_path="/bin/false"))
    changed_watchdog = _contract(
        runtime=_Runtime(cli_path="/bin/true", startup_timeout=9.0, idle_timeout=99.0)
    )
    assert baseline.fingerprint != changed_executable.fingerprint
    assert baseline.fingerprint != changed_watchdog.fingerprint


def test_runtime_command_builder_override_changes_authority() -> None:
    class CommandOne(_Runtime):
        def _build_command(self) -> list[str]:
            return ["cli", "one"]

    class CommandTwo(_Runtime):
        def _build_command(self) -> list[str]:
            return ["cli", "two"]

    assert (
        _contract(runtime=CommandOne()).fingerprint != _contract(runtime=CommandTwo()).fingerprint
    )


def test_in_place_executable_generation_change_changes_authority(tmp_path) -> None:
    executable = tmp_path / "runtime-cli"
    executable.write_text("one", encoding="utf-8")
    baseline = _contract(runtime=_Runtime(cli_path=str(executable)))
    previous = executable.stat()
    executable.write_text("two", encoding="utf-8")
    os.utime(
        executable,
        ns=(previous.st_atime_ns, max(executable.stat().st_mtime_ns, previous.st_mtime_ns + 1)),
    )
    changed = _contract(runtime=_Runtime(cli_path=str(executable)))
    assert baseline.fingerprint != changed.fingerprint


def test_executable_content_drift_changes_authority_with_restored_stat(tmp_path) -> None:
    executable = tmp_path / "runtime-cli"
    executable.write_text("one", encoding="utf-8")
    previous = executable.stat()
    baseline = _contract(runtime=_Runtime(cli_path=str(executable)))
    executable.write_text("two", encoding="utf-8")
    os.utime(executable, ns=(previous.st_atime_ns, previous.st_mtime_ns))
    changed = _contract(runtime=_Runtime(cli_path=str(executable)))
    assert baseline.fingerprint != changed.fingerprint


def test_non_executable_runtime_target_fails_closed_and_changes_authority(tmp_path) -> None:
    executable = tmp_path / "runtime-cli"
    executable.write_text("one", encoding="utf-8")
    os.chmod(executable, 0o755)
    baseline = _contract(runtime=_Runtime(cli_path=str(executable)))

    os.chmod(executable, 0o644)
    changed = _contract(runtime=_Runtime(cli_path=str(executable)))

    assert baseline.fingerprint != changed.fingerprint
    assert baseline.portable_across_processes is True
    assert changed.data["runtime"]["executable"]["observed"] is False
    assert changed.portable_across_processes is False


def test_runtime_execution_body_and_parser_drift_change_authority() -> None:
    class ImplementationOne(_Runtime):
        async def _execute_task_impl(self, **_: object):
            if False:  # pragma: no cover - implementation identity only
                yield "one"

        def _parse_json_event(self, _line: str) -> str:
            return "one"

        def _convert_event(self, _event: object) -> str:
            return "one"

    class ImplementationTwo(_Runtime):
        async def _execute_task_impl(self, **_: object):
            if False:  # pragma: no cover - implementation identity only
                yield "two"

        def _parse_json_event(self, _line: str) -> str:
            return "two"

        def _convert_event(self, _event: object) -> str:
            return "two"

    assert (
        _contract(runtime=ImplementationOne()).fingerprint
        != _contract(runtime=ImplementationTwo()).fingerprint
    )


def test_instance_level_execution_override_is_process_local() -> None:
    runtime = _Runtime()
    baseline = _contract(runtime=runtime)

    async def replacement(self: _Runtime, **_: object):
        if False:  # pragma: no cover - implementation identity only
            yield self.profile

    runtime.execute_task = MethodType(replacement, runtime)  # type: ignore[method-assign]
    overridden = _contract(runtime=runtime)
    assert baseline.fingerprint != overridden.fingerprint
    assert overridden.portable_across_processes is False

    runtime.execute_task = None  # type: ignore[method-assign]
    non_callable = _contract(runtime=runtime)
    assert baseline.fingerprint != non_callable.fingerprint
    assert non_callable.portable_across_processes is False

    runtime_with_handler = _Runtime()
    runtime_with_handler._handler = lambda: "one"  # type: ignore[attr-defined]
    handler_contract = _contract(runtime=runtime_with_handler)
    assert (
        handler_contract.data["runtime"]["implementation"]["instance_overrides"]["_handler"]["mode"]
        == "callable"
    )
    assert handler_contract.portable_across_processes is False


def test_bundled_worker_transport_policy_changes_authority() -> None:
    claude_first = _worker_contract(
        ClaudeWorkerTransport(
            cli_path="/usr/bin/true",
            timeout=1,
            disallowed_tools=("tool-a",),
            persist_sessions=False,
        ),
        backend="claude_mcp",
        llm_backend="claude",
    )
    claude_second = _worker_contract(
        ClaudeWorkerTransport(
            cli_path="/usr/bin/false",
            timeout=9,
            disallowed_tools=("tool-b",),
            persist_sessions=True,
        ),
        backend="claude_mcp",
        llm_backend="claude",
    )
    codex_first = _worker_contract(
        CodexMcpWorkerTransport(cli_path="/usr/bin/true", idle_timeout=1),
        backend="codex_mcp",
        llm_backend="codex",
    )
    codex_second = _worker_contract(
        CodexMcpWorkerTransport(cli_path="/usr/bin/false", idle_timeout=9),
        backend="codex_mcp",
        llm_backend="codex",
    )

    assert claude_first.fingerprint != claude_second.fingerprint
    assert codex_first.fingerprint != codex_second.fingerprint
    transport = claude_first.data["runtime"]["implementation"]["composition"]["components"][
        "transport"
    ]
    assert transport["mode"] == "declared"
    assert transport["executable"]["observed"] is True


def test_bundled_transport_identity_is_stable_and_binds_instance_override() -> None:
    transport = ClaudeWorkerTransport(cli_path="/usr/bin/true")
    runtime = LeaderDrivenWorkerRuntime(
        transport=transport,
        runtime_backend="claude_mcp",
        llm_backend="claude",
    )

    def build() -> ExecutionAuthorityContract:
        return ExecutionAuthorityContract.build(
            adapter=runtime,
            verifier=None,
            executor=_ExecutionOwner(),
            workspace="/tmp/workspace-a",
            workspace_generation=_generation(),
            execution_policy=_policy(backend="claude_mcp"),
        )

    first = build()
    assert first.fingerprint == build().fingerprint

    transport.spawn = lambda **_: None  # type: ignore[method-assign]
    overridden = build()
    assert first.fingerprint != overridden.fingerprint
    component = overridden.data["runtime"]["implementation"]["composition"]["components"][
        "transport"
    ]
    assert component["stability"] == "process_local"

    transport.spawn = None  # type: ignore[method-assign]
    non_callable = build()
    assert first.fingerprint != non_callable.fingerprint
    component = non_callable.data["runtime"]["implementation"]["composition"]["components"][
        "transport"
    ]
    assert component["instance_overrides"]["spawn"]["mode"] == "non_callable"


def test_undeclared_transport_fails_closed_without_colliding() -> None:
    class OpaqueTransport:
        backend_name = "opaque"

        async def spawn(self, **_: object) -> object:
            raise NotImplementedError

        async def resume(self, **_: object) -> object:
            raise NotImplementedError

    first = _worker_contract(
        OpaqueTransport(),
        backend="opaque",
        llm_backend="opaque",
    )
    second = _worker_contract(
        OpaqueTransport(),
        backend="opaque",
        llm_backend="opaque",
    )

    assert first.fingerprint != second.fingerprint
    component = first.data["runtime"]["implementation"]["composition"]["components"]["transport"]
    assert component["mode"] == "process_local"
    assert first.portable_across_processes is False


def test_malformed_transport_identity_fails_closed_without_leaking() -> None:
    class MalformedTransport:
        backend_name = "malformed"

        def execution_identity_contract(self) -> dict[str, object]:
            return {"version": True, "configuration": {"api_token": "sentinel-secret"}}

        async def spawn(self, **_: object) -> object:
            raise NotImplementedError

        async def resume(self, **_: object) -> object:
            raise NotImplementedError

    contract = _worker_contract(
        MalformedTransport(),
        backend="malformed",
        llm_backend="malformed",
    )
    component = contract.data["runtime"]["implementation"]["composition"]["components"]["transport"]
    assert component["mode"] == "process_local"
    assert "sentinel-secret" not in contract.canonical_json


def test_sensitive_transport_identity_fails_closed_without_leaking() -> None:
    class SensitiveTransport:
        backend_name = "sensitive"

        def execution_identity_contract(self) -> dict[str, object]:
            return {
                "version": 1,
                "configuration": {"api_token": "sentinel-secret"},
            }

        async def spawn(self, **_: object) -> object:
            raise NotImplementedError

        async def resume(self, **_: object) -> object:
            raise NotImplementedError

    contract = _worker_contract(
        SensitiveTransport(),
        backend="sensitive",
        llm_backend="sensitive",
    )

    component = contract.data["runtime"]["implementation"]["composition"]["components"]["transport"]
    assert component["mode"] == "process_local"
    assert "identity_digest" not in component
    assert "sentinel-secret" not in contract.canonical_json


def test_sensitive_runtime_identity_fails_closed_without_leaking() -> None:
    class SensitiveRuntime(_Runtime):
        def execution_identity_contract(self) -> dict[str, object]:
            return {
                "effective_model_observed": True,
                "api_token": "sentinel-secret",
            }

    contract = _contract(runtime=SensitiveRuntime())

    identity = contract.data["runtime"]["execution_identity"]
    assert identity["observed"] is False
    assert isinstance(identity["instance_nonce"], str)
    assert contract.portable_across_processes is False
    assert "sentinel-secret" not in contract.canonical_json


def test_sensitive_runtime_identity_value_fails_closed_without_leaking() -> None:
    class SensitiveRuntime(_Runtime):
        def execution_identity_contract(self) -> dict[str, object]:
            return {
                "effective_model_observed": True,
                "opaque": "sk-sentinel-secret",
            }

    contract = _contract(runtime=SensitiveRuntime())

    identity = contract.data["runtime"]["execution_identity"]
    assert identity["observed"] is False
    assert isinstance(identity["instance_nonce"], str)
    assert contract.portable_across_processes is False
    assert "sk-sentinel-secret" not in contract.canonical_json


def test_github_pat_runtime_identity_value_fails_closed_without_leaking() -> None:
    credential = "ghp_abcdefghijklmnopqrstuvwxyz1234567890"

    class SensitiveRuntime(_Runtime):
        def execution_identity_contract(self) -> dict[str, object]:
            return {
                "effective_model_observed": True,
                "opaque": credential,
            }

    contract = _contract(runtime=SensitiveRuntime())

    assert contract.data["runtime"]["execution_identity"]["observed"] is False
    assert credential not in contract.canonical_json


@pytest.mark.parametrize(
    "credential",
    (
        "glpat-sentinel-not-a-real-token-123456",
        "hf_abcdefghijklmnopqrstuvwxyz123456",
        "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJzZW50aW5lbCJ9.c2lnbmF0dXJl",
    ),
)
def test_structured_credential_runtime_identity_fails_closed_without_leaking(
    credential: str,
) -> None:
    class SensitiveRuntime(_Runtime):
        def execution_identity_contract(self) -> dict[str, object]:
            return {
                "effective_model_observed": True,
                "opaque": credential,
            }

    contract = _contract(runtime=SensitiveRuntime())

    assert contract.data["runtime"]["execution_identity"]["observed"] is False
    assert credential not in contract.canonical_json


def test_sensitive_runtime_identity_is_collision_resistant_per_runtime_instance() -> None:
    class SensitiveRuntime(_Runtime):
        def execution_identity_contract(self) -> dict[str, object]:
            return {
                "effective_model_observed": True,
                "opaque": "sk-sentinel-secret",
            }

    first_runtime = SensitiveRuntime()
    first = runtime_execution_identity_contract(first_runtime)
    same = runtime_execution_identity_contract(first_runtime)
    second = runtime_execution_identity_contract(SensitiveRuntime())

    assert first["observed"] is False
    assert second["observed"] is False
    assert first == same
    assert first["instance_nonce"] != second["instance_nonce"]


def test_process_local_nonce_is_stable_per_live_object_and_released_after_gc() -> None:
    class MissingIdentityRuntime:
        capabilities = FULL_CAPABILITIES
        runtime_backend = "legacy"
        llm_backend = "legacy"
        permission_mode = "default"

    runtime = MissingIdentityRuntime()
    object_id = id(runtime)
    first = runtime_execution_identity_contract(runtime)
    second = runtime_execution_identity_contract(runtime)
    runtime_reference = weakref.ref(runtime)

    assert first == second
    assert first["observed"] is False
    assert first["instance_nonce"]

    del runtime
    gc.collect()

    assert runtime_reference() is None
    assert object_id not in execution_authority_module._PROCESS_LOCAL_RUNTIME_NONCES


def test_credential_markers_never_egress_via_authority_metadata() -> None:
    credential = "ghp_abcdefghijklmnopqrstuvwxyz1234567890"

    class CredentialLabelRuntime(_Runtime):
        runtime_backend = credential
        llm_backend = credential
        permission_mode = credential
        _model = credential

    class LocalCredentialSkillRuntime(_Runtime):
        _skill_dispatcher = None
        _skills_dir = credential

        async def _maybe_dispatch_skill_intercept(self, **_: object) -> None:
            return None

        async def _dispatch_skill_intercept_locally(self, **_: object) -> None:
            return None

    dynamic_runtime_type = type(f"Runtime_{credential}", (_Runtime,), {})
    dynamic_runtime_type.__module__ = f"module_{credential}"

    async def named_execute(self: _Runtime, **_: object):
        if False:  # pragma: no cover - implementation identity only
            yield self.profile

    named_execute.__name__ = credential
    named_execute.__qualname__ = credential
    named_execute.__module__ = credential
    dynamic_function_runtime = _Runtime()
    dynamic_function_runtime.execute_task = MethodType(  # type: ignore[method-assign]
        named_execute,
        dynamic_function_runtime,
    )

    class CredentialComponentKeyRuntime(_Runtime):
        def execution_components(self) -> dict[str, object]:
            return {credential: _StableDispatcher()}

    credential_exception = type(f"ProviderError_{credential}", (RuntimeError,), {})

    class ExceptionNameRuntime(_Runtime):
        def watchdog_identity_contract(self) -> dict[str, object]:
            raise credential_exception()

    contracts = (
        _contract(runtime=CredentialLabelRuntime()),
        _contract(runtime=LocalCredentialSkillRuntime()),
        _contract(runtime=dynamic_runtime_type()),  # type: ignore[arg-type]
        _contract(runtime=dynamic_function_runtime),
        _contract(runtime=CredentialComponentKeyRuntime()),
        _contract(runtime=ExceptionNameRuntime()),
    )

    assert all(credential not in contract.canonical_json for contract in contracts)


def test_raising_runtime_identity_provider_blocks_construction_without_leaking() -> None:
    class RaisingRuntime(_Runtime):
        def execution_identity_contract(self) -> dict[str, object]:
            raise RuntimeError("sk-sentinel-secret")

    with pytest.raises(ValueError, match="runtime execution identity provider failed") as error:
        _contract(runtime=RaisingRuntime())

    assert "sentinel-secret" not in str(error.value)


def test_sensitive_executable_command_policy_fails_closed_without_leaking() -> None:
    class SensitiveExecutableRuntime(_Runtime):
        def __init__(self, secret: str) -> None:
            super().__init__()
            self._secret = secret

        def executable_identity_contract(self) -> dict[str, object]:
            return {
                "executable": None,
                "launcher": None,
                "command_policy": {"connection_label": self._secret},
            }

    contract = _contract(runtime=SensitiveExecutableRuntime("sk-sentinel-secret"))
    changed = _contract(runtime=SensitiveExecutableRuntime("sk-another-secret"))

    executable = contract.data["runtime"]["executable"]
    assert executable["required"] is True
    assert executable["observed"] is False
    assert contract.portable_across_processes is False
    assert contract.fingerprint != changed.fingerprint
    assert "sk-sentinel-secret" not in contract.canonical_json


def test_sensitive_watchdog_identity_fails_closed_without_leaking() -> None:
    contract = _contract(runtime=_SensitiveWatchdogRuntime())
    changed = _contract(runtime=_SensitiveWatchdogRuntime("sk-another-secret"))

    watchdog = contract.data["runtime"]["watchdog"]
    assert watchdog["required"] is True
    assert watchdog["observed"] is False
    assert contract.portable_across_processes is False
    assert contract.fingerprint != changed.fingerprint
    assert "sk-sentinel-secret" not in contract.canonical_json


def test_runtime_handle_selector_values_are_outside_baseline_without_leaking() -> None:
    class SensitiveHandleRuntime(_Runtime):
        def resume_handle_execution_identity_contract(
            self,
            runtime_handle: RuntimeHandle | None,
        ) -> dict[str, object]:
            del runtime_handle
            return {"connection_label": "sk-sentinel-secret"}

    contract = _contract(
        runtime=SensitiveHandleRuntime(),
        runtime_handle=RuntimeHandle(backend="codex_cli"),
    )

    selector = contract.data["runtime"]["handle_selector"]
    assert selector["mode"] == "declared"
    assert selector["stability"] == "durable"
    assert "sk-sentinel-secret" not in contract.canonical_json


def test_provider_attribute_lookup_fail_closed_without_leaking() -> None:
    class RaisingExecutableRuntime(_Runtime):
        @property
        def executable_identity_contract(self) -> object:
            raise RuntimeError("sk-sentinel-secret")

    class RaisingWatchdogRuntime(_Runtime):
        @property
        def watchdog_identity_contract(self) -> object:
            raise RuntimeError("sk-sentinel-secret")

    class RaisingHandleRuntime(_Runtime):
        @property
        def resume_handle_execution_identity_contract(self) -> object:
            raise RuntimeError("sk-sentinel-secret")

    executable_contract = _contract(runtime=RaisingExecutableRuntime())
    watchdog_contract = _contract(runtime=RaisingWatchdogRuntime())
    handle_contract = _contract(
        runtime=RaisingHandleRuntime(),
        runtime_handle=RuntimeHandle(backend="codex_cli"),
    )

    assert executable_contract.data["runtime"]["executable"]["observed"] is False
    assert watchdog_contract.data["runtime"]["watchdog"]["observed"] is False
    selector = handle_contract.data["runtime"]["handle_selector"]
    assert selector["mode"] == "declared"
    assert selector["stability"] == "process_local"
    assert all(
        "sentinel-secret" not in contract.canonical_json
        for contract in (executable_contract, watchdog_contract, handle_contract)
    )


def test_fallback_runtime_attribute_errors_fail_closed_without_leaking() -> None:
    class RaisingCliPathRuntime(_Runtime):
        @property
        def cli_path(self) -> object:
            raise RuntimeError("sk-sentinel-secret")

    class RaisingWatchdogFieldRuntime(_Runtime):
        def __init__(self) -> None:
            self.profile = "profile-a"
            self._cli_path = None

        @property
        def _startup_output_timeout_seconds(self) -> object:
            raise RuntimeError("sk-sentinel-secret")

    class RaisingDispatcherRuntime(_Runtime):
        @property
        def _skill_dispatcher(self) -> object:
            raise RuntimeError("sk-sentinel-secret")

    executable_contract = _contract(runtime=RaisingCliPathRuntime())
    watchdog_contract = _contract(runtime=RaisingWatchdogFieldRuntime())
    dispatcher_contract = _contract(runtime=RaisingDispatcherRuntime())

    assert executable_contract.data["runtime"]["executable"]["observed"] is False
    assert watchdog_contract.data["runtime"]["watchdog"]["observed"] is False
    assert dispatcher_contract.data["runtime"]["skill_dispatcher"]["stability"] == "process_local"
    assert all(
        "sentinel-secret" not in contract.canonical_json
        for contract in (executable_contract, watchdog_contract, dispatcher_contract)
    )


def test_selected_runtime_handles_do_not_change_the_portable_baseline() -> None:
    runtime = _Runtime()
    first = _contract(
        runtime=runtime,
        runtime_handle=RuntimeHandle(backend="codex_cli", native_session_id="session-a"),
    )
    second = _contract(
        runtime=runtime,
        runtime_handle=RuntimeHandle(backend="codex_cli", native_session_id="session-b"),
    )

    assert first.fingerprint == second.fingerprint
    selector = first.data["runtime"]["handle_selector"]
    assert selector["mode"] == "declared"
    assert selector["stability"] == "durable"


def test_codex_watchdog_identity_is_digested_before_authority_serialization(tmp_path) -> None:
    runtime = CodexCliRuntime(cli_path="/usr/bin/true", cwd=str(tmp_path), model="gpt-5")

    contract = _contract(  # type: ignore[arg-type]
        runtime=runtime,
        policy=_policy(backend="codex_cli"),
    )

    watchdog = contract.data["runtime"]["watchdog"]
    assert watchdog["required"] is True
    assert watchdog["observed"] is True
    assert watchdog["identity_digest"].startswith("sha256:")
    assert "child_session_environment_names" not in contract.canonical_json


def test_codex_transport_live_pool_is_excluded_from_static_identity() -> None:
    transport = CodexMcpWorkerTransport(cli_path="/usr/bin/true")
    baseline = _worker_contract(
        transport,
        backend="codex_mcp",
        llm_backend="codex",
    )
    transport._pool["session"] = object()  # type: ignore[assignment]
    changed_live_state = _worker_contract(
        transport,
        backend="codex_mcp",
        llm_backend="codex",
    )

    assert baseline.fingerprint == changed_live_state.fingerprint


def test_unobservable_slotted_instance_state_is_process_local() -> None:
    runtime = _SlottedRuntime(lambda: "one")
    baseline = _contract(runtime=runtime)  # type: ignore[arg-type]
    runtime.handler = lambda: "two"
    changed = _contract(runtime=runtime)  # type: ignore[arg-type]
    assert baseline.fingerprint != changed.fingerprint
    assert baseline.portable_across_processes is False
    assert changed.portable_across_processes is False


def test_verifier_identity_drift_changes_authority() -> None:
    first = _contract(verifier=_Verifier("judge-a"))
    same = _contract(verifier=_Verifier("judge-a"))
    changed = _contract(verifier=_Verifier("judge-b"))

    assert first.fingerprint == same.fingerprint
    assert first.fingerprint != changed.fingerprint
    assert first.portable_across_processes is True


def test_callable_verifier_helper_override_changes_authority() -> None:
    passing = _contract(verifier=_PassingHelperVerifier())
    failing = _contract(verifier=_FailingHelperVerifier())

    assert passing.fingerprint != failing.fingerprint
    assert passing.data["verifier"]["implementation"]["stability"] == "durable"
    assert failing.data["verifier"]["implementation"]["stability"] == "durable"


def test_self_referential_callable_verifier_fails_closed_without_recursing() -> None:
    first = _contract(verifier=_SelfReferentialVerifier())
    second = _contract(verifier=_SelfReferentialVerifier())

    assert first.fingerprint != second.fingerprint
    assert first.portable_across_processes is False
    implementation = first.data["verifier"]["implementation"]
    assert implementation["stability"] == "process_local"
    assert implementation["instance_overrides"]["helper"]["mode"] == "callable"


def test_undeclared_custom_verifier_is_process_local() -> None:
    def verifier(**_: object) -> VerifierVerdict:
        return VerifierVerdict(passed=True)

    first = _contract(verifier=verifier)
    second = _contract(verifier=verifier)

    assert first.portable_across_processes is False
    assert first.fingerprint != second.fingerprint


def test_workspace_and_policy_drift_change_authority() -> None:
    baseline = _contract()
    assert baseline.fingerprint != _contract(workspace="/tmp/workspace-b").fingerprint
    assert baseline.fingerprint != _contract(policy=_policy(ac_retry_attempts=3)).fingerprint


def test_sensitive_direct_workspace_is_process_local_without_egress() -> None:
    secret = "ghp_" + "a" * 36
    workspace = f"/tmp/{secret}"

    authority = canonical_workspace_authority(workspace, generation=_generation())
    contract = _contract(workspace=workspace)

    assert authority["observed"] is False
    assert authority["identity"]["mode"] == "process_local"
    assert authority["generation"] == {"observed": False}
    assert contract.data["workspace"]["observed"] is False
    assert contract.portable_across_processes is False
    assert secret not in contract.canonical_json
    assert secret not in str(authority)


def test_workspace_symlink_to_sensitive_target_is_process_local_without_egress(tmp_path) -> None:
    secret = "ghp_" + "a" * 36
    target = tmp_path / secret
    target.mkdir()
    safe_link = tmp_path / "safe-workspace"
    safe_link.symlink_to(target, target_is_directory=True)

    authority = canonical_workspace_authority(str(safe_link), generation=_generation())
    contract = _contract(workspace=str(safe_link))

    assert authority["observed"] is False
    assert contract.data["workspace"]["observed"] is False
    assert contract.portable_across_processes is False
    assert secret not in str(authority)
    assert secret not in contract.canonical_json


def test_workspace_identity_symlink_target_is_process_local_without_egress(tmp_path) -> None:
    secret = "ghp_" + "a" * 36
    target = tmp_path / secret
    target.mkdir()
    safe_link = tmp_path / "safe-repo-root"
    safe_link.symlink_to(target, target_is_directory=True)
    workspace = str(tmp_path / "workspace")
    identity = {
        "effective_cwd": workspace,
        "repo_root": str(safe_link),
        "worktree_path": str(tmp_path / "worktree"),
        "branch": "authority-a",
    }

    contract = ExecutionAuthorityContract.build(
        adapter=_Runtime(),
        verifier=None,
        executor=_ExecutionOwner(),
        workspace=workspace,
        workspace_identity=identity,
        workspace_generation=_generation(),
        execution_policy=_policy(),
    )

    assert contract.data["workspace"]["observed"] is False
    assert contract.portable_across_processes is False
    assert secret not in contract.canonical_json


@pytest.mark.parametrize("field_name", ("effective_cwd", "repo_root", "worktree_path", "branch"))
def test_sensitive_workspace_identity_fields_are_process_local_without_egress(
    field_name: str,
) -> None:
    secret = "ghp_" + "a" * 36
    workspace = "/tmp/workspace-a"
    identity = {
        "effective_cwd": workspace,
        "repo_root": "/tmp/repo-a",
        "worktree_path": "/tmp/worktree-a",
        "branch": "authority-a",
    }
    identity[field_name] = f"/tmp/{secret}" if field_name != "branch" else f"branch-{secret}"
    if field_name == "effective_cwd":
        workspace = str(identity[field_name])

    contract = ExecutionAuthorityContract.build(
        adapter=_Runtime(),
        verifier=None,
        executor=_ExecutionOwner(),
        workspace=workspace,
        workspace_identity=identity,
        workspace_generation=_generation(),
        execution_policy=_policy(),
    )

    assert contract.data["workspace"]["observed"] is False
    assert contract.portable_across_processes is False
    assert secret not in contract.canonical_json


def test_sensitive_workspace_generation_is_process_local_without_egress() -> None:
    secret = "ghp_" + "a" * 36
    contract = ExecutionAuthorityContract.build(
        adapter=_Runtime(),
        verifier=None,
        executor=_ExecutionOwner(),
        workspace="/tmp/workspace-a",
        workspace_generation={"version": 1, "kind": "test", "digest": secret},
        execution_policy=_policy(),
    )

    assert contract.data["workspace"]["observed"] is False
    assert contract.portable_across_processes is False
    assert secret not in contract.canonical_json


def test_normal_workspace_authority_remains_observed() -> None:
    authority = canonical_workspace_authority("/tmp/workspace-a", generation=_generation())

    assert authority["version"] == 1
    assert authority["observed"] is True
    assert authority["identity"]["mode"] == "direct"
    assert authority["identity"]["effective_cwd"].endswith("/tmp/workspace-a")
    assert authority["generation"] == {"observed": True, "identity": _generation()}


def test_workspace_generation_drift_changes_authority() -> None:
    assert (
        _contract(generation=_generation("a")).fingerprint
        != _contract(generation=_generation("b")).fingerprint
    )


def test_empty_workspace_generation_is_unobserved_and_not_portable() -> None:
    contract = _contract(generation={})
    assert contract.data["workspace"]["generation"] == {"observed": False}
    assert contract.portable_across_processes is False


def test_malformed_workspace_generation_is_rejected() -> None:
    with pytest.raises(ValueError, match="workspace generation identity is invalid"):
        _contract(generation={"version": 1, "kind": "test", "digest": "not-a-digest"})


@pytest.mark.parametrize("missing_key", sorted(_policy()))
def test_incomplete_execution_policy_is_rejected(missing_key: str) -> None:
    policy = _policy()
    del policy[missing_key]
    with pytest.raises(ValueError, match="invalid execution policy"):
        _contract(policy=policy)


def test_dispatch_rate_policy_drift_changes_executor_authority(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    runtime = _Runtime()
    runtime.working_directory = str(tmp_path)
    runtime.self_governs_rate_limit = False  # type: ignore[attr-defined]

    monkeypatch.setenv("OUROBOROS_TEST_RUNTIME_RPM", "1")
    first = ParallelACExecutor(
        adapter=runtime,  # type: ignore[arg-type]
        event_store=AsyncMock(),
        task_cwd=str(tmp_path),
    )
    monkeypatch.setenv("OUROBOROS_TEST_RUNTIME_RPM", "9")
    second = ParallelACExecutor(
        adapter=runtime,  # type: ignore[arg-type]
        event_store=AsyncMock(),
        task_cwd=str(tmp_path),
    )

    first_policy = first.execution_authority.data["execution_policy"]["dispatch_rate"]
    second_policy = second.execution_authority.data["execution_policy"]["dispatch_rate"]
    assert first_policy["requests_per_minute"] == 1
    assert second_policy["requests_per_minute"] == 9
    assert first.execution_authority.fingerprint != second.execution_authority.fingerprint


def test_executor_implementation_override_changes_authority(tmp_path) -> None:
    runtime = _Runtime()
    runtime.working_directory = str(tmp_path)
    rate_policy = ResolvedDispatchRatePolicy.resolve(
        backend="test-runtime",
        self_governs_rate_limit=False,
        requests_per_minute=1,
        tokens_per_minute=None,
    )
    kwargs = {
        "adapter": runtime,
        "event_store": AsyncMock(),
        "task_cwd": str(tmp_path),
        "dispatch_rate_policy": rate_policy,
    }

    baseline = ParallelACExecutor(**kwargs).execution_authority  # type: ignore[arg-type]
    bypassed = _BypassDispatchRateExecutor(**kwargs).execution_authority  # type: ignore[arg-type]

    assert baseline.data["execution_policy"]["dispatch_rate"]["gate_enabled"] is True
    assert baseline.fingerprint != bypassed.fingerprint
    assert baseline.data["executor"]["stability"] == "durable"
    assert bypassed.data["executor"]["stability"] == "durable"


def test_leaf_dispatcher_replacement_changes_executor_authority(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    runtime = _Runtime()
    runtime.working_directory = str(tmp_path)
    kwargs = {
        "adapter": runtime,
        "event_store": AsyncMock(),
        "task_cwd": str(tmp_path),
        "dispatch_rate_policy": ResolvedDispatchRatePolicy.resolve(
            backend="test-runtime",
            self_governs_rate_limit=False,
            requests_per_minute=1,
            tokens_per_minute=None,
        ),
    }

    baseline = ParallelACExecutor(**kwargs).execution_authority  # type: ignore[arg-type]
    monkeypatch.setattr(parallel_executor_module, "LeafDispatcher", _ReplacementLeafDispatcher)
    replaced = ParallelACExecutor(**kwargs).execution_authority  # type: ignore[arg-type]

    assert baseline.fingerprint != replaced.fingerprint
    component = replaced.data["executor"]["components"]["leaf_dispatcher"]
    assert component["mode"] == "static_type"
    # Dynamic module/class labels can contain provider-controlled text.  The
    # baseline keeps only an opaque type digest while still differentiating the
    # replacement implementation above.
    assert component["type"].startswith("sha256:")


def test_live_session_signal_hub_is_explicitly_volatile_not_baseline_identity(tmp_path) -> None:
    runtime = _Runtime()
    runtime.working_directory = str(tmp_path)
    kwargs = {
        "adapter": runtime,
        "event_store": AsyncMock(),
        "task_cwd": str(tmp_path),
        "dispatch_rate_policy": ResolvedDispatchRatePolicy.resolve(
            backend="test-runtime",
            self_governs_rate_limit=False,
            requests_per_minute=1,
            tokens_per_minute=None,
        ),
    }

    baseline = ParallelACExecutor(**kwargs).execution_authority  # type: ignore[arg-type]
    with_hub = ParallelACExecutor(
        **kwargs,
        session_signal_hub=SessionSignalHub(),
    ).execution_authority  # type: ignore[arg-type]

    assert baseline.fingerprint == with_hub.fingerprint
    assert "session_signal_hub" not in with_hub.data["executor"]["components"]
    assert "session_signal_hub" in with_hub.data["boundary"]["volatile"]


def test_level_coordinator_is_explicitly_per_attempt_not_baseline_identity(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    class SuppressingCoordinator(LevelCoordinator):
        def __init__(self, *args: object, **kwargs: object) -> None:
            super().__init__(*args, **kwargs)  # type: ignore[arg-type]
            self._reasoning_effort = "high"

        @staticmethod
        def detect_file_conflicts(_level_results: object) -> list[object]:
            return []

    runtime = _Runtime()
    runtime.working_directory = str(tmp_path)
    kwargs = {
        "adapter": runtime,
        "event_store": AsyncMock(),
        "task_cwd": str(tmp_path),
    }
    baseline = ParallelACExecutor(**kwargs).execution_authority  # type: ignore[arg-type]

    monkeypatch.setattr(parallel_executor_module, "LevelCoordinator", SuppressingCoordinator)
    changed = ParallelACExecutor(**kwargs).execution_authority  # type: ignore[arg-type]

    assert baseline.fingerprint == changed.fingerprint
    assert "level_coordinator" not in baseline.data["executor"]["components"]
    assert (
        "level_coordinator_behavior_and_session_state"
        in baseline.data["boundary"]["per_attempt_capsule"]
    )


def test_handle_manager_and_event_emitter_are_explicitly_outside_foundation_a(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    class ReplacingHandleManager(ACRuntimeHandleManager):
        pass

    class ReplacingEventEmitter(ExecutionEventEmitter):
        pass

    runtime = _Runtime()
    runtime.working_directory = str(tmp_path)
    kwargs = {
        "adapter": runtime,
        "event_store": AsyncMock(),
        "task_cwd": str(tmp_path),
    }
    baseline = ParallelACExecutor(**kwargs).execution_authority  # type: ignore[arg-type]

    monkeypatch.setattr(parallel_executor_module, "ACRuntimeHandleManager", ReplacingHandleManager)
    monkeypatch.setattr(parallel_executor_module, "ExecutionEventEmitter", ReplacingEventEmitter)
    changed = ParallelACExecutor(**kwargs).execution_authority  # type: ignore[arg-type]

    assert baseline.fingerprint == changed.fingerprint
    boundary = changed.data["boundary"]
    assert "ac_runtime_handle_manager" in boundary["per_attempt_capsule"]
    assert "execution_event_emitter" in boundary["volatile"]


def test_noncanonical_dispatch_rate_policy_is_rejected(tmp_path) -> None:
    class DormantRatePolicy(ResolvedDispatchRatePolicy):
        def build_gate(self):  # type: ignore[no-untyped-def]
            return ResolvedDispatchRatePolicy.resolve(
                backend=self.backend,
                self_governs_rate_limit=False,
                requests_per_minute=None,
                tokens_per_minute=None,
            ).build_gate()

    runtime = _Runtime()
    runtime.working_directory = str(tmp_path)
    policy = DormantRatePolicy(
        backend="test-runtime",
        owner="ouroboros",
        observed=True,
        self_governs_rate_limit=False,
        requests_per_minute=1,
        tokens_per_minute=None,
    )

    with pytest.raises(ValueError, match="canonical policy type"):
        ParallelACExecutor(
            adapter=runtime,  # type: ignore[arg-type]
            event_store=AsyncMock(),
            task_cwd=str(tmp_path),
            dispatch_rate_policy=policy,
        )


def test_dispatch_rate_gate_is_built_from_canonical_policy_fields(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    runtime = _Runtime()
    runtime.working_directory = str(tmp_path)
    policy = ResolvedDispatchRatePolicy(
        backend="test-runtime",
        owner="ouroboros",
        observed=True,
        self_governs_rate_limit=False,
        requests_per_minute=1,
        tokens_per_minute=23,
        window_seconds=17.0,
        heartbeat_seconds=3.0,
        max_wait_seconds=11.0,
    )

    def altered_builder(_: ResolvedDispatchRatePolicy):
        return build_rate_limit_gate(
            "test-runtime",
            request_limit=99,
            token_limit=None,
            window_seconds=1.0,
            heartbeat_seconds=1.0,
            max_wait_seconds=1.0,
        )

    monkeypatch.setattr(ResolvedDispatchRatePolicy, "build_gate", altered_builder)
    executor = ParallelACExecutor(
        adapter=runtime,  # type: ignore[arg-type]
        event_store=AsyncMock(),
        task_cwd=str(tmp_path),
        dispatch_rate_policy=policy,
    )

    bucket = executor._dispatch_rate_gate._bucket
    assert bucket._request_limit == 1
    assert bucket._token_limit == 23
    assert bucket._window_seconds == 17.0
    assert executor._dispatch_rate_gate._heartbeat_seconds == 3.0
    assert executor._dispatch_rate_gate._max_wait_seconds == 11.0
    rate_contract = executor.execution_authority.data["execution_policy"]["dispatch_rate"]
    assert rate_contract["gate_algorithm_version"] == 1


def test_actual_rate_gate_factory_drift_changes_authority(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    runtime = _Runtime()
    runtime.working_directory = str(tmp_path)
    policy = ResolvedDispatchRatePolicy(
        backend="test-runtime",
        owner="ouroboros",
        observed=True,
        self_governs_rate_limit=False,
        requests_per_minute=1,
        tokens_per_minute=23,
        window_seconds=17.0,
        heartbeat_seconds=3.0,
        max_wait_seconds=11.0,
    )
    kwargs = {
        "adapter": runtime,
        "event_store": AsyncMock(),
        "task_cwd": str(tmp_path),
        "dispatch_rate_policy": policy,
    }
    baseline = ParallelACExecutor(**kwargs)  # type: ignore[arg-type]

    def replaced_factory(*_: object, **__: object):
        return build_rate_limit_gate(
            "test-runtime",
            request_limit=99,
            token_limit=None,
            window_seconds=1.0,
            heartbeat_seconds=1.0,
            max_wait_seconds=1.0,
        )

    monkeypatch.setattr(rate_limit_module, "build_rate_limit_gate", replaced_factory)
    changed = ParallelACExecutor(**kwargs)  # type: ignore[arg-type]

    changed_bucket = changed._dispatch_rate_gate._bucket
    assert changed_bucket._request_limit == 99
    assert changed_bucket._token_limit is None
    assert changed_bucket._window_seconds == 1.0
    assert changed.execution_authority.fingerprint != baseline.execution_authority.fingerprint
    baseline_algorithm = baseline.execution_authority.data["execution_policy"]["dispatch_rate"][
        "gate_algorithm"
    ]
    changed_algorithm = changed.execution_authority.data["execution_policy"]["dispatch_rate"][
        "gate_algorithm"
    ]
    assert baseline_algorithm["observed"] is True
    assert changed_algorithm["observed"] is False
    assert baseline_algorithm != changed_algorithm


def test_rate_gate_factory_restoring_global_cannot_hide_its_authority_drift(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    """The factory capture must survive a replacement that restores the alias."""
    runtime = _Runtime()
    runtime.working_directory = str(tmp_path)
    policy = ResolvedDispatchRatePolicy.resolve(
        backend="test-runtime",
        self_governs_rate_limit=False,
        requests_per_minute=1,
        tokens_per_minute=23,
    )
    original_factory = rate_limit_module.build_rate_limit_gate

    def restoring_factory(*args: object, **kwargs: object) -> RateLimitGate:
        monkeypatch.setattr(rate_limit_module, "build_rate_limit_gate", original_factory)
        return original_factory(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(rate_limit_module, "build_rate_limit_gate", restoring_factory)
    executor = ParallelACExecutor(
        adapter=runtime,  # type: ignore[arg-type]
        event_store=AsyncMock(),
        task_cwd=str(tmp_path),
        dispatch_rate_policy=policy,
    )

    rate_contract = executor.execution_authority.data["execution_policy"]["dispatch_rate"]
    assert rate_limit_module.build_rate_limit_gate is original_factory
    assert executor._dispatch_rate_gate._bucket._request_limit == 1
    assert rate_contract["gate_algorithm"]["observed"] is False
    assert executor.execution_authority.portable_across_processes is False


def test_rate_token_estimator_is_captured_and_in_place_drift_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    """TPM accounting uses the same validated estimator captured in authority."""
    runtime = _Runtime()
    runtime.working_directory = str(tmp_path)
    policy = ResolvedDispatchRatePolicy.resolve(
        backend="test-runtime",
        self_governs_rate_limit=False,
        requests_per_minute=1,
        tokens_per_minute=100_000,
    )
    kwargs = {
        "adapter": runtime,
        "event_store": AsyncMock(),
        "task_cwd": str(tmp_path),
        "dispatch_rate_policy": policy,
    }
    baseline = ParallelACExecutor(**kwargs)  # type: ignore[arg-type]
    estimator = parallel_executor_module.estimate_runtime_request_tokens

    def altered_estimator(_prompt: str, *, system_prompt: str | None = None) -> int:
        del system_prompt
        return 1

    assert altered_estimator.__closure__ is None
    monkeypatch.setattr(estimator, "__code__", altered_estimator.__code__)
    changed = ParallelACExecutor(**kwargs)  # type: ignore[arg-type]

    baseline_estimator = baseline.execution_authority.data["execution_policy"]["dispatch_rate"][
        "token_estimator"
    ]
    changed_estimator = changed.execution_authority.data["execution_policy"]["dispatch_rate"][
        "token_estimator"
    ]
    assert baseline_estimator["observed"] is True
    assert changed_estimator["observed"] is False
    assert baseline.execution_authority.fingerprint != changed.execution_authority.fingerprint

    with pytest.raises(ValueError, match="token estimator drifted"):
        asyncio.run(
            baseline._await_dispatch_rate_budget(
                prompt="a prompt that would otherwise consume TPM budget",
                system_prompt=None,
            )
        )


def test_in_place_rate_gate_factory_code_drift_is_process_local(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    """An object-identical factory with changed code cannot become portable."""
    runtime = _Runtime()
    runtime.working_directory = str(tmp_path)
    policy = ResolvedDispatchRatePolicy.resolve(
        backend="test-runtime",
        self_governs_rate_limit=False,
        requests_per_minute=1,
        tokens_per_minute=None,
    )
    kwargs = {
        "adapter": runtime,
        "event_store": AsyncMock(),
        "task_cwd": str(tmp_path),
        "dispatch_rate_policy": policy,
    }
    baseline = ParallelACExecutor(**kwargs)  # type: ignore[arg-type]
    factory = rate_limit_module.build_rate_limit_gate

    def stochastic_factory(
        runtime_backend,
        *,
        request_limit,
        token_limit,
        window_seconds=60.0,
        max_wait_seconds=120.0,
        heartbeat_seconds=30.0,
        sleep=None,
    ):
        bucket = SharedRateLimitBucket(
            runtime_backend=runtime_backend,
            request_limit=random.choice((1, 99)),
            token_limit=token_limit,
            window_seconds=window_seconds,
        )
        return RateLimitGate(
            bucket,
            max_wait_seconds=max_wait_seconds,
            heartbeat_seconds=heartbeat_seconds,
            sleep=sleep,
        )

    assert stochastic_factory.__closure__ is None
    monkeypatch.setattr(rate_limit_module, "random", random, raising=False)
    monkeypatch.setattr(factory, "__code__", stochastic_factory.__code__)

    random.seed(0)
    first = ParallelACExecutor(**kwargs)  # type: ignore[arg-type]
    random.seed(1)
    second = ParallelACExecutor(**kwargs)  # type: ignore[arg-type]

    assert factory is rate_limit_module.build_rate_limit_gate
    assert (
        first._dispatch_rate_gate._bucket._request_limit
        != second._dispatch_rate_gate._bucket._request_limit
    )
    baseline_algorithm = baseline.execution_authority.data["execution_policy"]["dispatch_rate"][
        "gate_algorithm"
    ]
    first_algorithm = first.execution_authority.data["execution_policy"]["dispatch_rate"][
        "gate_algorithm"
    ]
    second_algorithm = second.execution_authority.data["execution_policy"]["dispatch_rate"][
        "gate_algorithm"
    ]
    assert baseline_algorithm["observed"] is True
    assert first_algorithm["observed"] is False
    assert second_algorithm["observed"] is False
    assert first_algorithm != second_algorithm


def test_in_place_rate_gate_factory_default_drift_is_process_local(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Mutating the original factory's keyword defaults is declaration drift."""
    factory = rate_limit_module.build_rate_limit_gate
    kwdefaults = factory.__kwdefaults__
    assert kwdefaults is not None

    monkeypatch.setitem(kwdefaults, "window_seconds", 1.0)

    algorithm = rate_limit_module.rate_limit_gate_algorithm_contract()
    assert algorithm["observed"] is False


def test_in_place_rate_gate_member_implementation_drift_is_process_local(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A gate member's code can drift without replacing its function object."""
    member = rate_limit_module.SharedRateLimitBucket._tokens_in_window

    def altered_tokens_in_window(self):
        return 99

    assert altered_tokens_in_window.__closure__ is None
    monkeypatch.setattr(member, "__code__", altered_tokens_in_window.__code__)

    algorithm = rate_limit_module.rate_limit_gate_algorithm_contract()
    assert algorithm["observed"] is False


def test_in_place_rate_gate_member_default_drift_is_process_local(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The exact-declaration rule also binds original member defaults."""
    member = rate_limit_module.RateLimitGate.acquire
    kwdefaults = member.__kwdefaults__
    assert kwdefaults is not None

    monkeypatch.setitem(kwdefaults, "on_backoff", lambda _event: None)

    algorithm = rate_limit_module.rate_limit_gate_algorithm_contract()
    assert algorithm["observed"] is False


def test_in_place_rate_gate_result_type_drift_is_process_local(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Directly constructed gate result types cannot drift under a baseline."""
    original_init = rate_limit_module.RateLimitSnapshot.__init__
    offset = {"value": 99}

    def altered_init(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        original_init(self, *args, **kwargs)
        object.__setattr__(
            self,
            "requests_in_window",
            self.requests_in_window + offset["value"],
        )

    monkeypatch.setattr(rate_limit_module.RateLimitSnapshot, "__init__", altered_init)
    bucket = rate_limit_module.SharedRateLimitBucket(
        runtime_backend="test-runtime",
        request_limit=1,
        token_limit=None,
    )
    assert bucket._snapshot().requests_in_window == 99

    algorithm = rate_limit_module.rate_limit_gate_algorithm_contract()
    assert algorithm["observed"] is False


def test_in_place_asyncio_lock_implementation_drift_is_process_local(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A changed stdlib lock implementation cannot retain portable identity."""
    member = rate_limit_module.asyncio.Lock.acquire

    async def bypass_lock(self):
        return True

    assert bypass_lock.__closure__ is None
    monkeypatch.setattr(member, "__code__", bypass_lock.__code__)

    algorithm = rate_limit_module.rate_limit_gate_algorithm_contract()
    assert algorithm["observed"] is False


def test_in_place_asyncio_sleep_implementation_drift_is_process_local(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A changed stdlib scheduling function cannot retain portable identity."""
    sleep = rate_limit_module.asyncio.sleep

    async def bypass_sleep(delay, result=None):
        return result

    assert bypass_sleep.__closure__ is None
    monkeypatch.setattr(sleep, "__code__", bypass_sleep.__code__)

    algorithm = rate_limit_module.rate_limit_gate_algorithm_contract()
    assert algorithm["observed"] is False


def test_asyncio_lock_global_dependency_drift_is_process_local(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A lock member's direct module global is part of the declaration."""
    original_collections = rate_limit_module.asyncio.locks.collections
    monkeypatch.setattr(
        rate_limit_module.asyncio.locks,
        "collections",
        SimpleNamespace(deque=original_collections.deque),
    )

    algorithm = rate_limit_module.rate_limit_gate_algorithm_contract()
    assert algorithm["observed"] is False


def test_asyncio_lock_module_member_drift_is_process_local(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Mutating a direct module member used by a lock mixin fails closed."""
    events = rate_limit_module.asyncio.events
    original_get_running_loop = events._get_running_loop

    def altered_get_running_loop():
        return original_get_running_loop()

    monkeypatch.setattr(events, "_get_running_loop", altered_get_running_loop)

    algorithm = rate_limit_module.rate_limit_gate_algorithm_contract()
    assert algorithm["observed"] is False


def test_asyncio_lock_collections_member_drift_is_process_local(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A direct collections member used by lock acquisition is declaration-bound."""
    monkeypatch.setattr(rate_limit_module.asyncio.locks.collections, "deque", list)

    algorithm = rate_limit_module.rate_limit_gate_algorithm_contract()
    assert algorithm["observed"] is False


def test_asyncio_sleep_module_member_drift_is_process_local(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sleep's direct event-loop lookup is part of its imported declaration."""
    events = rate_limit_module.asyncio.events
    original_get_running_loop = events.get_running_loop

    def altered_get_running_loop():
        return original_get_running_loop()

    monkeypatch.setattr(events, "get_running_loop", altered_get_running_loop)

    algorithm = rate_limit_module.rate_limit_gate_algorithm_contract()
    assert algorithm["observed"] is False


def test_asyncio_sleep_global_function_drift_is_process_local(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sleep's direct helper function body is part of its declaration."""
    helper = rate_limit_module.asyncio.tasks.__sleep0

    @coroutine
    def altered_sleep_zero():
        if False:  # pragma: no cover - preserves the generator shape.
            yield None
        return None

    assert altered_sleep_zero.__closure__ is None
    monkeypatch.setattr(helper, "__code__", altered_sleep_zero.__code__)

    algorithm = rate_limit_module.rate_limit_gate_algorithm_contract()
    assert algorithm["observed"] is False


def test_rate_gate_replacements_and_live_dependencies_are_process_local(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    """Replacement factories and live gate dependencies stay process-local."""
    runtime = _Runtime()
    runtime.working_directory = str(tmp_path)
    policy = ResolvedDispatchRatePolicy(
        backend="test-runtime",
        owner="ouroboros",
        observed=True,
        self_governs_rate_limit=False,
        requests_per_minute=1,
        tokens_per_minute=23,
        window_seconds=17.0,
        heartbeat_seconds=3.0,
        max_wait_seconds=11.0,
    )
    kwargs = {
        "adapter": runtime,
        "event_store": AsyncMock(),
        "task_cwd": str(tmp_path),
        "dispatch_rate_policy": policy,
    }

    def closure_factory(limit: int):
        def factory(*_: object, **__: object):
            return build_rate_limit_gate(
                "test-runtime",
                request_limit=limit,
                token_limit=None,
                window_seconds=1.0,
                heartbeat_seconds=1.0,
                max_wait_seconds=1.0,
            )

        return factory

    closure_nine = closure_factory(9)
    closure_ninety_nine = closure_factory(99)
    assert closure_nine.__code__ is closure_ninety_nine.__code__
    monkeypatch.setattr(rate_limit_module, "build_rate_limit_gate", closure_nine)
    closure_first = ParallelACExecutor(**kwargs)  # type: ignore[arg-type]
    monkeypatch.setattr(rate_limit_module, "build_rate_limit_gate", closure_ninety_nine)
    closure_second = ParallelACExecutor(**kwargs)  # type: ignore[arg-type]
    assert closure_first._dispatch_rate_gate._bucket._request_limit == 9
    assert closure_second._dispatch_rate_gate._bucket._request_limit == 99
    assert closure_first.execution_authority.fingerprint != closure_second.execution_authority.fingerprint
    closure_first_algorithm = closure_first.execution_authority.data["execution_policy"][
        "dispatch_rate"
    ]["gate_algorithm"]
    closure_second_algorithm = closure_second.execution_authority.data["execution_policy"][
        "dispatch_rate"
    ]["gate_algorithm"]
    assert closure_first_algorithm["observed"] is False
    assert closure_second_algorithm["observed"] is False

    def default_factory(limit: int):
        def factory(*_: object, request_limit: int = limit, **__: object):
            return build_rate_limit_gate(
                "test-runtime",
                request_limit=request_limit,
                token_limit=None,
                window_seconds=1.0,
                heartbeat_seconds=1.0,
                max_wait_seconds=1.0,
            )

        return factory

    default_nine = default_factory(9)
    default_ninety_nine = default_factory(99)
    assert default_nine.__code__ is default_ninety_nine.__code__
    monkeypatch.setattr(rate_limit_module, "build_rate_limit_gate", default_nine)
    default_first = ParallelACExecutor(**kwargs)  # type: ignore[arg-type]
    monkeypatch.setattr(rate_limit_module, "build_rate_limit_gate", default_ninety_nine)
    default_second = ParallelACExecutor(**kwargs)  # type: ignore[arg-type]
    assert default_first.execution_authority.fingerprint != default_second.execution_authority.fingerprint
    default_first_algorithm = default_first.execution_authority.data["execution_policy"][
        "dispatch_rate"
    ]["gate_algorithm"]
    default_second_algorithm = default_second.execution_authority.data["execution_policy"][
        "dispatch_rate"
    ]["gate_algorithm"]
    assert default_first_algorithm["observed"] is False
    assert default_second_algorithm["observed"] is False

    monkeypatch.setitem(_RATE_GATE_FACTORY_TEST_GLOBALS, "limit", 9)

    def global_factory(*_: object, **__: object):
        return build_rate_limit_gate(
            "test-runtime",
            request_limit=_RATE_GATE_FACTORY_TEST_GLOBALS["limit"],
            token_limit=None,
            window_seconds=1.0,
            heartbeat_seconds=1.0,
            max_wait_seconds=1.0,
        )

    monkeypatch.setattr(rate_limit_module, "build_rate_limit_gate", global_factory)
    global_first = ParallelACExecutor(**kwargs)  # type: ignore[arg-type]
    monkeypatch.setitem(_RATE_GATE_FACTORY_TEST_GLOBALS, "limit", 99)
    global_second = ParallelACExecutor(**kwargs)  # type: ignore[arg-type]
    assert global_first._dispatch_rate_gate._bucket._request_limit == 9
    assert global_second._dispatch_rate_gate._bucket._request_limit == 99
    assert global_first.execution_authority.fingerprint != global_second.execution_authority.fingerprint
    global_first_algorithm = global_first.execution_authority.data["execution_policy"]["dispatch_rate"][
        "gate_algorithm"
    ]
    global_second_algorithm = global_second.execution_authority.data["execution_policy"][
        "dispatch_rate"
    ]["gate_algorithm"]
    assert global_first_algorithm["observed"] is False
    assert global_second_algorithm["observed"] is False

    monkeypatch.setattr(rate_limit_module, "build_rate_limit_gate", build_rate_limit_gate)
    class_baseline = ParallelACExecutor(**kwargs)  # type: ignore[arg-type]
    original_bucket_init = rate_limit_module.SharedRateLimitBucket.__init__

    def altered_bucket_init(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        original_bucket_init(self, *args, **kwargs)
        self._request_limit = 99

    monkeypatch.setattr(
        rate_limit_module.SharedRateLimitBucket,
        "__init__",
        altered_bucket_init,
    )
    class_changed = ParallelACExecutor(**kwargs)  # type: ignore[arg-type]
    assert class_baseline._dispatch_rate_gate._bucket._request_limit == 1
    assert class_changed._dispatch_rate_gate._bucket._request_limit == 99
    assert class_baseline.execution_authority.fingerprint != class_changed.execution_authority.fingerprint

    monkeypatch.setitem(_RATE_GATE_FACTORY_TEST_GLOBALS, "limit", 9)

    @wraps(original_bucket_init)
    def wrapped_bucket_init(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        original_bucket_init(self, *args, **kwargs)
        self._request_limit = _RATE_GATE_FACTORY_TEST_GLOBALS["limit"]

    monkeypatch.setattr(
        rate_limit_module.SharedRateLimitBucket,
        "__init__",
        wrapped_bucket_init,
    )
    class_global_first = ParallelACExecutor(**kwargs)  # type: ignore[arg-type]
    monkeypatch.setitem(_RATE_GATE_FACTORY_TEST_GLOBALS, "limit", 99)
    class_global_second = ParallelACExecutor(**kwargs)  # type: ignore[arg-type]
    assert class_global_first._dispatch_rate_gate._bucket._request_limit == 9
    assert class_global_second._dispatch_rate_gate._bucket._request_limit == 99
    assert class_global_first.execution_authority.fingerprint != class_global_second.execution_authority.fingerprint
    first_algorithm = class_global_first.execution_authority.data["execution_policy"][
        "dispatch_rate"
    ]["gate_algorithm"]
    second_algorithm = class_global_second.execution_authority.data["execution_policy"][
        "dispatch_rate"
    ]["gate_algorithm"]
    assert first_algorithm["observed"] is False
    assert second_algorithm["observed"] is False
    assert first_algorithm != second_algorithm

    monkeypatch.setattr(
        rate_limit_module.SharedRateLimitBucket,
        "__init__",
        original_bucket_init,
    )
    original_monotonic = rate_limit_module.time.monotonic
    monotonic_offset = {"value": 0.0}

    def patched_monotonic() -> float:
        return original_monotonic() + monotonic_offset["value"]

    monkeypatch.setattr(rate_limit_module.time, "monotonic", patched_monotonic)
    module_first = ParallelACExecutor(**kwargs)  # type: ignore[arg-type]
    first_timestamp = module_first._dispatch_rate_gate._bucket._time()
    monotonic_offset["value"] = 99.0
    module_second = ParallelACExecutor(**kwargs)  # type: ignore[arg-type]
    second_timestamp = module_second._dispatch_rate_gate._bucket._time()
    assert second_timestamp - first_timestamp > 98.0
    assert module_first.execution_authority.fingerprint != module_second.execution_authority.fingerprint
    module_algorithm = module_first.execution_authority.data["execution_policy"][
        "dispatch_rate"
    ]["gate_algorithm"]
    assert module_algorithm["observed"] is False

    monkeypatch.setattr(rate_limit_module.time, "monotonic", original_monotonic)
    original_deque = rate_limit_module.deque
    deque_offset = {"value": 0}

    class OffsetDeque(list):
        def append(self, item: tuple[float, int]) -> None:
            super().append((item[0], item[1] + deque_offset["value"]))

    monkeypatch.setattr(rate_limit_module, "deque", OffsetDeque)
    deque_first = ParallelACExecutor(**kwargs)  # type: ignore[arg-type]
    deque_first._dispatch_rate_gate._bucket._reservations.append((0.0, 1))
    assert deque_first._dispatch_rate_gate._bucket._tokens_in_window() == 1
    deque_offset["value"] = 99
    deque_second = ParallelACExecutor(**kwargs)  # type: ignore[arg-type]
    deque_second._dispatch_rate_gate._bucket._reservations.append((0.0, 1))
    assert deque_second._dispatch_rate_gate._bucket._tokens_in_window() == 100
    assert deque_first.execution_authority.fingerprint != deque_second.execution_authority.fingerprint
    deque_algorithm = deque_first.execution_authority.data["execution_policy"]["dispatch_rate"][
        "gate_algorithm"
    ]
    assert deque_algorithm["observed"] is False

    monkeypatch.setattr(rate_limit_module, "deque", original_deque)
    descriptor_baseline = ParallelACExecutor(**kwargs)  # type: ignore[arg-type]
    original_gate_acquire = rate_limit_module.RateLimitGate.acquire

    class InertAcquireDescriptor:
        def __get__(self, _instance: object, _owner: object):
            async def noop(*_args: object, **_kwargs: object) -> None:
                return None

            return noop

    monkeypatch.setattr(rate_limit_module.RateLimitGate, "acquire", InertAcquireDescriptor())
    descriptor_changed = ParallelACExecutor(**kwargs)  # type: ignore[arg-type]
    assert descriptor_baseline.execution_authority.fingerprint != descriptor_changed.execution_authority.fingerprint
    descriptor_algorithm = descriptor_changed.execution_authority.data["execution_policy"][
        "dispatch_rate"
    ]["gate_algorithm"]
    assert descriptor_algorithm["observed"] is False

    monkeypatch.setattr(rate_limit_module.RateLimitGate, "acquire", original_gate_acquire)
    spoof_baseline = ParallelACExecutor(**kwargs)  # type: ignore[arg-type]

    class SpoofAcquireStaticMethod(staticmethod):
        def __get__(self, _instance: object, _owner: object):
            async def noop(*_args: object, **_kwargs: object) -> None:
                return None

            return noop

    monkeypatch.setattr(
        rate_limit_module.RateLimitGate,
        "acquire",
        SpoofAcquireStaticMethod(original_gate_acquire),
    )
    spoof_changed = ParallelACExecutor(**kwargs)  # type: ignore[arg-type]
    assert spoof_baseline.execution_authority.fingerprint != spoof_changed.execution_authority.fingerprint
    spoof_algorithm = spoof_changed.execution_authority.data["execution_policy"]["dispatch_rate"][
        "gate_algorithm"
    ]
    assert spoof_algorithm["observed"] is False

    monkeypatch.setattr(rate_limit_module.RateLimitGate, "acquire", original_gate_acquire)
    raw_class_baseline = ParallelACExecutor(**kwargs)  # type: ignore[arg-type]

    def bypassing_getattribute(self: object, name: str):  # type: ignore[no-untyped-def]
        if name == "acquire":
            async def noop(*_args: object, **_kwargs: object) -> None:
                return None

            return noop
        return object.__getattribute__(self, name)

    monkeypatch.setattr(
        rate_limit_module.RateLimitGate,
        "__getattribute__",
        bypassing_getattribute,
        raising=False,
    )
    raw_class_changed = ParallelACExecutor(**kwargs)  # type: ignore[arg-type]
    assert raw_class_baseline.execution_authority.fingerprint != raw_class_changed.execution_authority.fingerprint
    raw_class_algorithm = raw_class_changed.execution_authority.data["execution_policy"][
        "dispatch_rate"
    ]["gate_algorithm"]
    assert raw_class_algorithm["observed"] is False


def test_delegated_skill_interceptor_configuration_changes_authority(tmp_path) -> None:
    first_runtime = PiRuntime(
        cli_path="/usr/bin/true",
        cwd=tmp_path,
        skills_dir=tmp_path / "skills-a",
    )
    second_runtime = PiRuntime(
        cli_path="/usr/bin/true",
        cwd=tmp_path,
        skills_dir=tmp_path / "skills-b",
    )

    first = ExecutionAuthorityContract.build(
        adapter=first_runtime,
        verifier=None,
        executor=_ExecutionOwner(),
        workspace=str(tmp_path),
        workspace_generation=_generation(),
        execution_policy=_policy(backend="pi"),
    )
    second = ExecutionAuthorityContract.build(
        adapter=second_runtime,
        verifier=None,
        executor=_ExecutionOwner(),
        workspace=str(tmp_path),
        workspace_generation=_generation(),
        execution_policy=_policy(backend="pi"),
    )

    skill_dispatcher = first.data["runtime"]["skill_dispatcher"]
    assert skill_dispatcher["mode"] == "delegated"
    assert skill_dispatcher["component"]["mode"] == "declared"
    assert skill_dispatcher["stability"] == "durable"
    assert first.fingerprint != second.fingerprint


def test_self_governing_rate_policy_is_explicit_and_not_portable(tmp_path) -> None:
    runtime = _Runtime()
    runtime.working_directory = str(tmp_path)
    runtime.self_governs_rate_limit = True  # type: ignore[attr-defined]
    executor = ParallelACExecutor(
        adapter=runtime,  # type: ignore[arg-type]
        event_store=AsyncMock(),
        task_cwd=str(tmp_path),
    )
    rate_policy = executor.execution_authority.data["execution_policy"]["dispatch_rate"]
    assert rate_policy["owner"] == "runtime"
    assert rate_policy["observed"] is False
    assert executor._dispatch_rate_gate.enabled is False
    assert executor.execution_authority.portable_across_processes is False


def test_dispatch_rate_backend_must_match_runtime_backend() -> None:
    policy = _policy()
    rate_policy = dict(policy["dispatch_rate"])  # type: ignore[arg-type]
    rate_policy["backend"] = "foreign-runtime"
    policy["dispatch_rate"] = rate_policy
    with pytest.raises(ValueError, match="disagrees with the runtime backend"):
        _contract(policy=policy)


def test_dispatch_rate_self_governance_must_match_runtime() -> None:
    runtime = _Runtime()
    runtime.self_governs_rate_limit = True  # type: ignore[attr-defined]
    with pytest.raises(ValueError, match="disagrees with runtime self-governance"):
        _contract(runtime=runtime, policy=_policy())


def test_unobserved_workspace_or_runtime_is_not_reusable() -> None:
    class UnobservedRuntime:
        capabilities = FULL_CAPABILITIES
        runtime_backend = "legacy"
        llm_backend = None
        permission_mode = None
        working_directory = None

    contract = ExecutionAuthorityContract.build(
        adapter=UnobservedRuntime(),
        verifier=None,
        workspace=None,
        execution_policy=_policy(backend="legacy"),
    )
    assert contract.portable_across_processes is False


def test_selected_runtime_handle_belongs_to_the_later_attempt_capsule() -> None:
    cheap = RuntimeHandle(backend="codex_cli", metadata={"profile": "cheap"})
    expensive = RuntimeHandle(backend="codex_cli", metadata={"profile": "expensive"})
    assert _contract(runtime_handle=cheap).fingerprint == _contract(runtime_handle=expensive).fingerprint


def test_verifier_identity_provider_failure_degrades_to_process_local() -> None:
    class RaisingVerifier:
        def verification_identity_contract(self) -> dict[str, object]:
            raise OSError("offline")

        def __call__(self, **_: object) -> VerifierVerdict:
            return VerifierVerdict(passed=True)

    contract = _contract(verifier=RaisingVerifier())
    assert contract.portable_across_processes is False
    assert contract.data["verifier"]["behavioral_state"]["stability"] == "process_local"


def test_runtime_transcript_verifier_has_implementation_identity() -> None:
    verifier = _contract().data["verifier"]
    assert verifier["mode"] == "runtime_transcript"
    assert verifier["implementation"]["stability"] == "durable"


def test_runtime_transcript_helper_drift_changes_authority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    baseline = _contract().fingerprint

    def always_support_claim(_value: str, _messages: tuple[object, ...]) -> bool:
        return True

    monkeypatch.setattr(
        transcript_verification,
        "_runtime_messages_support_claim",
        always_support_claim,
    )
    changed = _contract().fingerprint

    assert baseline != changed


def test_runtime_transcript_verdict_type_drift_changes_authority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class ForgedVerdict:
        def __init__(self, **_: object) -> None:
            self.passed = True

    baseline = _contract().fingerprint
    monkeypatch.setattr(transcript_verification, "VerifierVerdict", ForgedVerdict)
    changed = _contract().fingerprint

    assert baseline != changed


def test_parallel_executor_exposes_one_authority_snapshot(tmp_path) -> None:
    runtime = _Runtime()
    runtime.working_directory = str(tmp_path)
    executor = ParallelACExecutor(
        adapter=runtime,  # type: ignore[arg-type]
        event_store=AsyncMock(),
        console=MagicMock(),
        task_cwd=str(tmp_path),
        execution_profile=load_profile("code"),
        atomic_verifier=_Verifier("judge-a"),
        ac_retry_attempts=2,
    )

    authority = executor.execution_authority
    assert authority.fingerprint.startswith("sha256:")
    assert authority.data["workspace"]["identity"]["effective_cwd"] == str(tmp_path.resolve())
    execution_identity = authority.data["runtime"]["execution_identity"]
    assert execution_identity["effective_model_observed"] is True
    assert execution_identity["identity_digest"].startswith("sha256:")
    assert "profile-a" not in authority.canonical_json
    assert authority.data["execution_policy"]["ac_retry_attempts"] == 2


def test_profile_policy_drift_changes_executor_authority(tmp_path) -> None:
    runtime = _Runtime()
    runtime.working_directory = str(tmp_path)
    base_profile = load_profile("code")
    changed_profile = base_profile.model_copy(update={"profile": "code-v2"})

    def build(profile: ExecutionProfile) -> str:
        return ParallelACExecutor(
            adapter=runtime,  # type: ignore[arg-type]
            event_store=AsyncMock(),
            console=MagicMock(),
            task_cwd=str(tmp_path),
            execution_profile=profile,
        ).execution_authority.fingerprint

    assert build(base_profile) != build(changed_profile)


def test_transcript_verifier_wrapper_override_changes_executor_authority(tmp_path) -> None:
    class OverriddenExecutor(ParallelACExecutor):
        def _verify_atomic_evidence_against_runtime_messages(self, **kwargs: object):  # type: ignore[no-untyped-def]
            return super()._verify_atomic_evidence_against_runtime_messages(**kwargs)  # type: ignore[arg-type]

    runtime = _Runtime()
    runtime.working_directory = str(tmp_path)
    kwargs = {
        "adapter": runtime,
        "event_store": AsyncMock(),
        "console": MagicMock(),
        "task_cwd": str(tmp_path),
        "execution_profile": load_profile("code"),
    }
    baseline = ParallelACExecutor(**kwargs).execution_authority.fingerprint  # type: ignore[arg-type]
    overridden = OverriddenExecutor(**kwargs).execution_authority.fingerprint  # type: ignore[arg-type]
    assert baseline != overridden


def test_shadow_replay_binds_transcript_wrapper_with_custom_verifier(tmp_path) -> None:
    class OverriddenExecutor(ParallelACExecutor):
        def _verify_atomic_evidence_against_runtime_messages(self, **kwargs: object):  # type: ignore[no-untyped-def]
            return super()._verify_atomic_evidence_against_runtime_messages(**kwargs)  # type: ignore[arg-type]

    runtime = _Runtime()
    runtime.working_directory = str(tmp_path)
    kwargs = {
        "adapter": runtime,
        "event_store": AsyncMock(),
        "console": MagicMock(),
        "task_cwd": str(tmp_path),
        "execution_profile": load_profile("code"),
        "atomic_verifier": _Verifier("judge-a"),
        "shadow_replay_enabled": True,
    }
    baseline = ParallelACExecutor(**kwargs).execution_authority.fingerprint  # type: ignore[arg-type]
    overridden = OverriddenExecutor(**kwargs).execution_authority.fingerprint  # type: ignore[arg-type]
    assert baseline != overridden


def test_resolved_runtime_authority_rejects_adapter_mismatch() -> None:
    runtime = _Runtime(profile="actual")
    resolved_routing = {
        "runtime_backend": "test-runtime",
        "llm_backend": "test-llm",
        "permission_mode": {"observed": True, "mode": "bypassPermissions"},
        "constructor_model": {"observed": True, "model": None},
        "runtime_execution": {
            "version": 1,
            "observed": True,
            "identity": {"profile": "fake", "effective_model_observed": True},
        },
    }
    with pytest.raises(ValueError, match="runtime_execution"):
        ResolvedRuntimeAuthority.bind(runtime, resolved_routing)


def test_bound_runtime_authority_is_used_without_raw_mapping_injection() -> None:
    runtime = _Runtime(profile="persisted")
    resolved_routing = {
        "runtime_backend": "test-runtime",
        "llm_backend": "test-llm",
        "permission_mode": {"observed": True, "mode": "bypassPermissions"},
        "constructor_model": {"observed": True, "model": None},
        "runtime_execution": runtime.execution_identity_contract(),
    }
    resolved_routing["runtime_execution"] = {
        "version": 1,
        "observed": True,
        "identity": resolved_routing["runtime_execution"],
    }
    bound = ResolvedRuntimeAuthority.bind(runtime, resolved_routing)
    contract = ExecutionAuthorityContract.build(
        adapter=runtime,
        verifier=None,
        executor=_ExecutionOwner(),
        workspace="/tmp/workspace-a",
        workspace_generation=_generation("a"),
        execution_policy=_policy(),
        resolved_routing=bound,
    )
    identity = contract.data["runtime"]["execution_identity"]
    assert identity["effective_model_observed"] is True
    assert identity["identity_digest"].startswith("sha256:")
    assert "persisted" not in contract.canonical_json


def test_bound_runtime_authority_rejects_a_different_adapter_instance() -> None:
    original = _Runtime(profile="same")
    replacement = _Runtime(profile="same")
    resolved_routing = {
        "runtime_backend": "test-runtime",
        "llm_backend": "test-llm",
        "permission_mode": {"observed": True, "mode": "bypassPermissions"},
        "constructor_model": {"observed": True, "model": None},
        "runtime_execution": {
            "version": 1,
            "observed": True,
            "identity": original.execution_identity_contract(),
        },
    }
    bound = ResolvedRuntimeAuthority.bind(original, resolved_routing)
    with pytest.raises(ValueError, match="different adapter"):
        ExecutionAuthorityContract.build(
            adapter=replacement,
            verifier=None,
            workspace="/tmp/workspace-a",
            workspace_generation=_generation("a"),
            execution_policy=_policy(),
            resolved_routing=bound,
        )


def test_bound_runtime_authority_rejects_same_adapter_identity_drift() -> None:
    runtime = _Runtime(profile="before")
    resolved_routing = {
        "runtime_backend": "test-runtime",
        "llm_backend": "test-llm",
        "permission_mode": {"observed": True, "mode": "bypassPermissions"},
        "constructor_model": {"observed": True, "model": None},
        "runtime_execution": {
            "version": 1,
            "observed": True,
            "identity": runtime.execution_identity_contract(),
        },
    }
    bound = ResolvedRuntimeAuthority.bind(runtime, resolved_routing)
    runtime.profile = "after"
    with pytest.raises(ValueError, match="drifted from active runtime_execution"):
        ExecutionAuthorityContract.build(
            adapter=runtime,
            verifier=None,
            workspace="/tmp/workspace-a",
            workspace_generation=_generation("a"),
            execution_policy=_policy(),
            resolved_routing=bound,
        )


def test_bound_runtime_authority_rejects_same_adapter_sensitive_label_drift() -> None:
    runtime = _Runtime(profile="stable")
    runtime.runtime_backend = "ghp_" + "a" * 36
    runtime.llm_backend = "ghp_" + "b" * 36
    resolved_routing = {
        "runtime_backend": None,
        "runtime_backend_unobserved": True,
        "llm_backend": None,
        "llm_backend_unobserved": True,
        "permission_mode": {"observed": True, "mode": "bypassPermissions"},
        "constructor_model": {"observed": True, "model": None},
        "runtime_execution": {
            "version": 1,
            "observed": True,
            "identity": runtime.execution_identity_contract(),
        },
    }
    bound = ResolvedRuntimeAuthority.bind(runtime, resolved_routing)
    runtime.runtime_backend = "ghp_" + "c" * 36

    with pytest.raises(ValueError, match="unobserved backend label"):
        bound.require_adapter(runtime)


def test_bound_runtime_authority_rejects_sensitive_execution_identity_drift() -> None:
    """A hidden provider identity remains bound for the initial live dispatch."""

    class SensitiveRuntime(_Runtime):
        def __init__(self) -> None:
            super().__init__(profile="stable")
            self.provider_identity = "ghp_" + "a" * 36

        def execution_identity_contract(self) -> dict[str, object]:
            return {
                "effective_model_observed": True,
                "provider_identity": self.provider_identity,
            }

    runtime = SensitiveRuntime()
    resolved_routing = {
        "runtime_backend": "test-runtime",
        "llm_backend": "test-llm",
        "permission_mode": {"observed": True, "mode": "bypassPermissions"},
        "constructor_model": {"observed": True, "model": None},
        "runtime_execution": runtime_execution_identity_contract(
            runtime,
            include_process_local_nonce=False,
        ),
    }
    bound = ResolvedRuntimeAuthority.bind(runtime, resolved_routing)

    assert resolved_routing["runtime_execution"] == {"version": 1, "observed": False}
    assert runtime.provider_identity not in bound.canonical_json

    runtime.provider_identity = "ghp_" + "b" * 36
    with pytest.raises(ValueError, match="unobserved execution identity"):
        bound.require_adapter(runtime)


def test_resolved_runtime_authority_reads_effective_private_permission_mode() -> None:
    runtime = _Runtime()
    runtime._permission_mode = "acceptEdits"  # type: ignore[attr-defined]
    resolved_routing = {
        "runtime_backend": "test-runtime",
        "llm_backend": "test-llm",
        "permission_mode": {"observed": True, "mode": "bypassPermissions"},
        "constructor_model": {"observed": True, "model": None},
        "runtime_execution": {
            "version": 1,
            "observed": True,
            "identity": runtime.execution_identity_contract(),
        },
    }
    with pytest.raises(ValueError, match="permission_mode"):
        ResolvedRuntimeAuthority.bind(runtime, resolved_routing)


def test_local_skill_fallback_is_process_local() -> None:
    class LocalSkillRuntime(_Runtime):
        _skill_dispatcher = None
        _skills_dir = "/tmp/skills"

        async def _maybe_dispatch_skill_intercept(self, **_: object) -> None:
            return None

        async def _dispatch_skill_intercept_locally(self, **_: object) -> None:
            return None

    contract = _contract(runtime=LocalSkillRuntime())
    assert contract.portable_across_processes is False
    assert contract.data["runtime"]["skill_dispatcher"]["mode"] == "local_fallback"


def test_declared_callable_dispatcher_has_durable_implementation_identity() -> None:
    runtime = _Runtime()
    runtime._skill_dispatcher = _StableDispatcher()  # type: ignore[attr-defined]
    contract = _contract(runtime=runtime)
    dispatcher = contract.data["runtime"]["skill_dispatcher"]
    assert dispatcher["stability"] == "durable"
    assert dispatcher["implementation"]["stability"] == "durable"


def test_dispatcher_identity_provider_failure_degrades_to_process_local() -> None:
    class RaisingDispatcher:
        @property
        def execution_identity_contract(self) -> object:
            raise OSError("offline")

        async def __call__(self, **_: object) -> None:
            return None

    runtime = _Runtime()
    runtime._skill_dispatcher = RaisingDispatcher()  # type: ignore[attr-defined]
    contract = _contract(runtime=runtime)
    assert contract.portable_across_processes is False
    assert contract.data["runtime"]["skill_dispatcher"]["stability"] == "process_local"
