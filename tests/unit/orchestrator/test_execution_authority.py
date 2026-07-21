from __future__ import annotations

import hashlib
import os
from types import MethodType
from unittest.mock import AsyncMock, MagicMock

import pytest

from ouroboros.orchestrator.adapter import FULL_CAPABILITIES, RuntimeHandle
from ouroboros.orchestrator.claude_worker_runtime import ClaudeWorkerTransport
from ouroboros.orchestrator.codex_mcp_runtime import CodexMcpWorkerTransport
from ouroboros.orchestrator.execution_authority import (
    ExecutionAuthorityContract,
    ResolvedRuntimeAuthority,
    build_execution_policy_contract,
)
from ouroboros.orchestrator.parallel_executor import ParallelACExecutor
from ouroboros.orchestrator.pi_runtime import PiRuntime
from ouroboros.orchestrator.profile_loader import ExecutionProfile, load_profile
from ouroboros.orchestrator.rate_limit import ResolvedDispatchRatePolicy
from ouroboros.orchestrator.verifier import VerifierVerdict
from ouroboros.orchestrator.worker_runtime import LeaderDrivenWorkerRuntime


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


class _BypassDispatchRateExecutor(ParallelACExecutor):
    async def _await_dispatch_rate_budget(
        self,
        *,
        prompt: str,
        system_prompt: str | None,
    ) -> None:
        del prompt, system_prompt


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


def test_runtime_handle_selector_changes_authority() -> None:
    cheap = RuntimeHandle(backend="codex_cli", metadata={"profile": "cheap"})
    expensive = RuntimeHandle(backend="codex_cli", metadata={"profile": "expensive"})
    assert (
        _contract(runtime_handle=cheap).fingerprint
        != _contract(runtime_handle=expensive).fingerprint
    )


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
    assert authority.data["runtime"]["execution_identity"]["identity"]["profile"] == "profile-a"
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
    assert contract.data["runtime"]["execution_identity"]["identity"]["profile"] == "persisted"


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
