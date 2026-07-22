from __future__ import annotations

from collections.abc import AsyncIterator
import gc
import json
import shutil
import sys
from unittest.mock import AsyncMock, MagicMock
import weakref

import pytest

from ouroboros.orchestrator.adapter import FULL_CAPABILITIES
from ouroboros.orchestrator.codex_cli_runtime import CodexCliRuntime
from ouroboros.orchestrator.copilot_cli_runtime import CopilotCliRuntime
from ouroboros.orchestrator.execution_authority import ExecutionAuthorityContract
import ouroboros.orchestrator.parallel_executor as parallel_executor_module
from ouroboros.orchestrator.parallel_executor import ParallelACExecutor
from ouroboros.orchestrator.profile_loader import EvidenceSchema, ExecutionProfile, load_profile
from ouroboros.orchestrator.verifier import VerifierVerdict, structural_atomic_verifier
from ouroboros.orchestrator.zcode_cli_runtime import ZcodeCLIRuntime


class _Runtime:
    capabilities = FULL_CAPABILITIES
    runtime_backend = "test-runtime"
    llm_backend = "test-llm"
    permission_mode = "bypassPermissions"
    working_directory: str | None = None
    _model = None

    def __init__(self, *, profile: str = "profile-a") -> None:
        self.profile = profile

    def execution_identity_contract(self) -> dict[str, object]:
        return {
            "profile": self.profile,
            "effective_model_observed": True,
        }

    async def execute_task(self, **_: object) -> AsyncIterator[object]:
        if False:  # pragma: no cover - implementation identity only
            yield None


class _DynamicDispatchRuntime:
    """A runtime whose dispatch entrypoint cannot be statically bound."""

    capabilities = FULL_CAPABILITIES
    runtime_backend = "dynamic-runtime"
    llm_backend = "dynamic-llm"
    permission_mode = "bypassPermissions"
    self_governs_rate_limit = False

    def execution_identity_contract(self) -> dict[str, object]:
        return {"kind": "dynamic-runtime/v1", "effective_model_observed": True}

    def __getattr__(self, name: str) -> object:
        if name == "execute_task":

            async def dispatch(**_: object) -> AsyncIterator[object]:
                if False:  # pragma: no cover - identity-only stand-in
                    yield None

            return dispatch
        raise AttributeError(name)


class _Verifier:
    def __init__(self, identity: str) -> None:
        self.identity = identity

    def verification_identity_contract(self) -> dict[str, object]:
        return {"judge": self.identity}

    def __call__(self, **_: object) -> VerifierVerdict:
        return VerifierVerdict(passed=True)


def _contract(
    *,
    runtime: object | None = None,
    verifier: object | None = None,
    workspace: str = "/tmp/workspace-a",
    policy: dict[str, object] | None = None,
) -> ExecutionAuthorityContract:
    return ExecutionAuthorityContract.build(
        adapter=runtime or _Runtime(),
        verifier=verifier,  # type: ignore[arg-type]
        workspace=workspace,
        execution_policy=policy or {"retry_attempts": 2},
    )


def test_runtime_profile_drift_changes_authority() -> None:
    assert (
        _contract(runtime=_Runtime(profile="a")).fingerprint
        != _contract(runtime=_Runtime(profile="b")).fingerprint
    )


def test_custom_verifier_is_never_promoted_to_portable_authority() -> None:
    first = _contract(verifier=_Verifier("judge-a"))
    same = _contract(verifier=_Verifier("judge-a"))
    changed = _contract(verifier=_Verifier("judge-b"))

    assert first.fingerprint != same.fingerprint
    assert first.fingerprint != changed.fingerprint
    assert first.portable_across_processes is False
    assert "judge-a" not in first.canonical_json


def test_undeclared_custom_verifier_is_process_local() -> None:
    def verifier(**_: object) -> VerifierVerdict:
        return VerifierVerdict(passed=True)

    first = _contract(verifier=verifier)
    second = _contract(verifier=verifier)

    assert first.reusable_across_processes is False
    assert first.fingerprint != second.fingerprint


def test_workspace_and_policy_drift_change_authority() -> None:
    baseline = _contract()
    assert baseline.fingerprint != _contract(workspace="/tmp/workspace-b").fingerprint
    assert baseline.fingerprint != _contract(policy={"retry_attempts": 3}).fingerprint


def test_closed_builtin_verifier_can_be_portable() -> None:
    runtime = CodexCliRuntime(
        cli_path=sys.executable,
        model="test-model",
        cwd="/tmp",
    )
    authority = _contract(runtime=runtime, verifier=structural_atomic_verifier)

    assert authority.portable_across_processes is True
    assert authority.data["verifier"] == {
        "implementation": "structural-atomic-verifier/v1",
        "mode": "structural_atomic",
        "stability": "durable",
        "version": 1,
    }


def test_unknown_runtime_implementation_stays_process_local_and_unique() -> None:
    class AlternateRuntime(_Runtime):
        async def execute_task(self, **_: object) -> AsyncIterator[object]:
            if False:  # pragma: no cover - implementation identity only
                yield None

    first = _contract(runtime=_Runtime())
    second = _contract(runtime=AlternateRuntime())

    assert first.portable_across_processes is False
    assert second.portable_across_processes is False
    assert first.fingerprint != second.fingerprint


def test_runtime_type_name_spoof_cannot_claim_closed_implementation() -> None:
    class SpoofedCodexRuntime(CodexCliRuntime):
        pass

    SpoofedCodexRuntime.__module__ = CodexCliRuntime.__module__
    SpoofedCodexRuntime.__qualname__ = CodexCliRuntime.__qualname__
    runtime = SpoofedCodexRuntime(
        cli_path=sys.executable,
        model="test-model",
        cwd="/tmp",
    )

    authority = _contract(runtime=runtime, verifier=structural_atomic_verifier)

    assert authority.portable_across_processes is False


def test_preconstruction_builtin_dispatch_patch_stays_process_local(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def altered_execute_task(*_: object, **__: object) -> AsyncIterator[object]:
        if False:  # pragma: no cover - implementation identity only
            yield None

    monkeypatch.setattr(CodexCliRuntime, "execute_task", altered_execute_task)
    runtime = CodexCliRuntime(
        cli_path=sys.executable,
        model="test-model",
        cwd="/tmp",
    )

    authority = _contract(runtime=runtime, verifier=structural_atomic_verifier)

    assert authority.portable_across_processes is False


def test_closed_codex_runtime_binds_executable_and_timeout_configuration() -> None:
    baseline = _contract(
        runtime=CodexCliRuntime(
            cli_path=sys.executable,
            model="test-model",
            cwd="/tmp",
            stdout_idle_timeout_seconds=30,
        ),
        verifier=structural_atomic_verifier,
    )
    timeout_changed = _contract(
        runtime=CodexCliRuntime(
            cli_path=sys.executable,
            model="test-model",
            cwd="/tmp",
            stdout_idle_timeout_seconds=31,
        ),
        verifier=structural_atomic_verifier,
    )

    assert baseline.portable_across_processes is True
    assert timeout_changed.portable_across_processes is True
    assert baseline.fingerprint != timeout_changed.fingerprint

    working_directory_changed = _contract(
        runtime=CodexCliRuntime(
            cli_path=sys.executable,
            model="test-model",
            cwd="/",
            stdout_idle_timeout_seconds=30,
        ),
        verifier=structural_atomic_verifier,
    )
    assert working_directory_changed.portable_across_processes is True
    assert baseline.fingerprint != working_directory_changed.fingerprint

    alternate_executable = shutil.which("true")
    if alternate_executable is not None and alternate_executable != sys.executable:
        executable_changed = _contract(
            runtime=CodexCliRuntime(
                cli_path=alternate_executable,
                model="test-model",
                cwd="/tmp",
                stdout_idle_timeout_seconds=30,
            ),
            verifier=structural_atomic_verifier,
        )
        assert executable_changed.portable_across_processes is True
        assert baseline.fingerprint != executable_changed.fingerprint


def test_closed_codex_runtime_custom_skill_configuration_stays_process_local(
    tmp_path,
) -> None:
    custom_skills = _contract(
        runtime=CodexCliRuntime(
            cli_path=sys.executable,
            model="test-model",
            cwd="/tmp",
            skills_dir=tmp_path,
        ),
        verifier=structural_atomic_verifier,
    )
    custom_dispatcher = _contract(
        runtime=CodexCliRuntime(
            cli_path=sys.executable,
            model="test-model",
            cwd="/tmp",
            skill_dispatcher=MagicMock(),
        ),
        verifier=structural_atomic_verifier,
    )

    assert custom_skills.portable_across_processes is False
    assert custom_dispatcher.portable_across_processes is False


def test_copilot_runtime_binds_resolved_profile_agent() -> None:
    plain = _contract(
        runtime=CopilotCliRuntime(
            cli_path=sys.executable,
            model="test-model",
            cwd="/tmp",
        ),
        verifier=structural_atomic_verifier,
    )
    worker_runtime = CopilotCliRuntime(
        cli_path=sys.executable,
        model="test-model",
        cwd="/tmp",
        runtime_profile="worker",
    )
    worker = _contract(runtime=worker_runtime, verifier=structural_atomic_verifier)

    assert plain.portable_across_processes is False
    assert worker.portable_across_processes is False
    assert plain.data["runtime"]["configuration"] != worker.data["runtime"]["configuration"]

    worker_runtime._copilot_agent = "different-agent"
    changed = _contract(runtime=worker_runtime, verifier=structural_atomic_verifier)
    assert changed.data["runtime"]["configuration"] != worker.data["runtime"]["configuration"]


def test_zcode_runtime_with_external_launcher_chain_stays_process_local() -> None:
    runtime = ZcodeCLIRuntime(cli_path=sys.executable, cwd="/tmp")

    authority = _contract(runtime=runtime, verifier=structural_atomic_verifier)

    assert authority.portable_across_processes is False
    assert authority.data["runtime"]["configuration"] == {"observed": False}


def test_credential_shaped_runtime_identity_becomes_process_local_without_egress() -> None:
    secret = "gh" + "p_abcdefghijklmnopqrstuvwxyz1234567890"
    authority = _contract(runtime=_Runtime(profile=secret))

    assert authority.portable_across_processes is False
    assert authority.data["runtime"]["stability"] == "process_local"
    assert secret not in authority.canonical_json


def test_stripe_credential_shaped_runtime_identity_becomes_process_local() -> None:
    secret = "sk_" + "live_abcdefghijklmnopqrstuvwxyz123456"
    authority = _contract(runtime=_Runtime(profile=secret))

    assert authority.portable_across_processes is False
    assert authority.data["runtime"]["execution_identity"] == {"observed": False}
    assert secret not in authority.canonical_json


def test_credential_alias_in_runtime_identity_becomes_process_local_without_egress() -> None:
    secret = "gh" + "p_abcdefghijklmnopqrstuvwxyz1234567890"

    class CredentialRuntime(_Runtime):
        def execution_identity_contract(self) -> dict[str, object]:
            return {
                "apiKeyValue": secret,
                "effective_model_observed": True,
            }

    authority = _contract(runtime=CredentialRuntime())

    assert authority.portable_across_processes is False
    assert authority.data["runtime"]["execution_identity"] == {"observed": False}
    assert secret not in authority.canonical_json


@pytest.mark.parametrize(
    "key_name",
    (
        "secretKey",
        "secret_access_key",
        "awsSecretAccessKey",
        "accountKey",
        "connectionString",
        "masterKey",
    ),
)
def test_secret_key_alias_in_runtime_identity_becomes_process_local(
    key_name: str,
) -> None:
    secret = "opaque-provider-credential"

    class CredentialRuntime(_Runtime):
        def execution_identity_contract(self) -> dict[str, object]:
            return {
                key_name: secret,
                "effective_model_observed": True,
            }

    authority = _contract(runtime=CredentialRuntime())

    assert authority.portable_across_processes is False
    assert authority.data["runtime"]["stability"] == "process_local"
    assert authority.data["runtime"]["execution_identity"] == {"observed": False}
    assert secret not in authority.canonical_json


def test_custom_verifier_credential_alias_never_enters_authority_json() -> None:
    secret = "gh" + "p_abcdefghijklmnopqrstuvwxyz1234567890"

    class CredentialVerifier(_Verifier):
        def verification_identity_contract(self) -> dict[str, object]:
            return {"apiKeyValue": secret}

    authority = _contract(verifier=CredentialVerifier("ignored"))

    assert authority.portable_across_processes is False
    assert authority.data["verifier"]["configuration"] == {"observed": False}
    assert secret not in authority.canonical_json


def test_contract_deserialization_rejects_credential_shaped_member() -> None:
    authority = _contract(verifier=structural_atomic_verifier)
    data = authority.data
    data["verifier"]["implementation"] = "gh" + "p_abcdefghijklmnopqrstuvwxyz1234567890"

    with pytest.raises(ValueError, match="sensitive"):
        ExecutionAuthorityContract(json.dumps(data, sort_keys=True, separators=(",", ":")))


def test_contract_deserialization_rejects_promoted_process_local_runtime() -> None:
    authority = _contract(runtime=_DynamicDispatchRuntime())
    data = authority.data
    assert data["runtime"]["portable_identity_observed"] is False
    data["runtime"]["stability"] = "durable"

    with pytest.raises(ValueError, match="invalid runtime"):
        ExecutionAuthorityContract(json.dumps(data, sort_keys=True, separators=(",", ":")))


def test_empty_runtime_identity_stays_process_local() -> None:
    class EmptyIdentityRuntime(_Runtime):
        def execution_identity_contract(self) -> dict[str, object]:
            return {}

    authority = _contract(runtime=EmptyIdentityRuntime())

    assert authority.portable_across_processes is False
    assert authority.data["runtime"]["stability"] == "process_local"
    assert authority.data["runtime"]["execution_identity"] == {"observed": False}


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
    assert authority.data["workspace"]["identity_digest"].startswith("sha256:")
    assert authority.data["runtime"]["execution_identity"]["digest"].startswith("sha256:")
    assert authority.data["execution_policy"]["identity_digest"].startswith("sha256:")


def test_session_signal_hub_is_process_local_and_cannot_drift(tmp_path) -> None:
    runtime = _Runtime()
    runtime.working_directory = str(tmp_path)
    hub = MagicMock()
    executor = ParallelACExecutor(
        adapter=runtime,  # type: ignore[arg-type]
        event_store=AsyncMock(),
        console=MagicMock(),
        task_cwd=str(tmp_path),
        session_signal_hub=hub,
    )

    assert executor.execution_authority.portable_across_processes is False
    assert executor._execution_authority_policy()["session_signal_hub_enabled"] is True
    executor._require_execution_authority_intact()

    executor._session_signal_hub = MagicMock()
    with pytest.raises(ValueError, match="execution authority drifted"):
        executor._require_execution_authority_intact()


def test_executor_rejects_copilot_agent_drift(tmp_path) -> None:
    runtime = CopilotCliRuntime(
        cli_path=sys.executable,
        model="test-model",
        cwd=tmp_path,
        runtime_profile="worker",
    )
    executor = ParallelACExecutor(
        adapter=runtime,
        event_store=AsyncMock(),
        console=MagicMock(),
        task_cwd=str(tmp_path),
    )

    assert executor.execution_authority.portable_across_processes is False
    executor._require_execution_authority_intact()

    runtime._copilot_agent = "different-agent"
    with pytest.raises(ValueError, match="execution authority drifted"):
        executor._require_execution_authority_intact()


def test_execution_authority_registry_does_not_keep_executor_alive(tmp_path) -> None:
    runtime = _Runtime()
    runtime.working_directory = str(tmp_path)
    executor = ParallelACExecutor(
        adapter=runtime,  # type: ignore[arg-type]
        event_store=AsyncMock(),
        console=MagicMock(),
        task_cwd=str(tmp_path),
    )
    executor_ref = weakref.ref(executor)

    del executor
    gc.collect()
    gc.collect()

    assert executor_ref() is None


def test_public_constructor_rejects_caller_supplied_closed_roots(tmp_path) -> None:
    runtime = _Runtime()
    runtime.working_directory = str(tmp_path)

    with pytest.raises(TypeError, match="multiple values"):
        ParallelACExecutor(
            adapter=runtime,  # type: ignore[arg-type, call-arg]
            event_store=AsyncMock(),
            console=MagicMock(),
            task_cwd=str(tmp_path),
            _foundation_a_roots=object(),  # type: ignore[call-arg]
        )


def test_constructor_closure_ignores_a_poisoned_global_roots_bundle(tmp_path) -> None:
    runtime = _Runtime()
    runtime.working_directory = str(tmp_path)
    roots = parallel_executor_module._FOUNDATION_A_CLOSED_ROOTS
    original_verifier = roots.transcript_verifier
    original_verifier_code = roots.transcript_verifier_code

    def injected_verifier(**_: object) -> VerifierVerdict:
        return VerifierVerdict(passed=False, reasons=("poisoned root",))

    object.__setattr__(roots, "transcript_verifier", injected_verifier)
    object.__setattr__(roots, "transcript_verifier_code", injected_verifier.__code__)
    try:
        executor = ParallelACExecutor(
            adapter=runtime,  # type: ignore[arg-type]
            event_store=AsyncMock(),
            console=MagicMock(),
            task_cwd=str(tmp_path),
        )
    finally:
        object.__setattr__(roots, "transcript_verifier", original_verifier)
        object.__setattr__(roots, "transcript_verifier_code", original_verifier_code)

    assert executor._authority_transcript_verifier is original_verifier
    assert executor._authority_transcript_verifier is not injected_verifier
    assert executor.execution_authority.portable_across_processes is False


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


def test_executor_rejects_runtime_descriptor_drift_before_effect(tmp_path) -> None:
    runtime = _Runtime()
    runtime.working_directory = str(tmp_path)
    executor = ParallelACExecutor(
        adapter=runtime,  # type: ignore[arg-type]
        event_store=AsyncMock(),
        console=MagicMock(),
        task_cwd=str(tmp_path),
    )
    executor._require_execution_authority_intact()

    runtime.profile = "changed"

    with pytest.raises(ValueError, match="execution authority drifted"):
        executor._require_execution_authority_intact()


def test_executor_rejects_replaced_verifier_root_before_effect(tmp_path) -> None:
    runtime = _Runtime()
    runtime.working_directory = str(tmp_path)
    executor = ParallelACExecutor(
        adapter=runtime,  # type: ignore[arg-type]
        event_store=AsyncMock(),
        console=MagicMock(),
        task_cwd=str(tmp_path),
        atomic_verifier=_Verifier("first"),
    )
    executor._require_execution_authority_intact()

    executor._atomic_verifier = _Verifier("replacement")

    with pytest.raises(ValueError, match="execution authority drifted"):
        executor._require_execution_authority_intact()


def test_executor_rejects_replaced_dispatcher_root_before_effect(tmp_path) -> None:
    runtime = _Runtime()
    runtime.working_directory = str(tmp_path)
    executor = ParallelACExecutor(
        adapter=runtime,  # type: ignore[arg-type]
        event_store=AsyncMock(),
        console=MagicMock(),
        task_cwd=str(tmp_path),
    )
    executor._require_execution_authority_intact()

    executor._authority_leaf_dispatcher_type = object

    with pytest.raises(ValueError, match="execution authority drifted"):
        executor._require_execution_authority_intact()


def test_executor_rejects_replaced_runtime_dispatch_root_before_effect(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    runtime = _Runtime()
    runtime.working_directory = str(tmp_path)
    executor = ParallelACExecutor(
        adapter=runtime,  # type: ignore[arg-type]
        event_store=AsyncMock(),
        console=MagicMock(),
        task_cwd=str(tmp_path),
    )
    executor._require_execution_authority_intact()

    async def replacement_execute_task(self: _Runtime, **_: object) -> AsyncIterator[object]:
        del self
        if False:  # pragma: no cover - implementation identity only
            yield None

    monkeypatch.setattr(_Runtime, "execute_task", replacement_execute_task)

    with pytest.raises(ValueError, match="execution authority drifted"):
        executor._require_execution_authority_intact()


def test_executor_rejects_replaced_dispatcher_method_root_before_effect(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    runtime = _Runtime()
    runtime.working_directory = str(tmp_path)
    executor = ParallelACExecutor(
        adapter=runtime,  # type: ignore[arg-type]
        event_store=AsyncMock(),
        console=MagicMock(),
        task_cwd=str(tmp_path),
    )
    executor._require_execution_authority_intact()

    async def replacement_stream(self: object, **_: object) -> None:
        del self

    monkeypatch.setattr(
        executor._authority_leaf_dispatcher_type,
        "stream",
        replacement_stream,
    )

    with pytest.raises(ValueError, match="execution authority drifted"):
        executor._require_execution_authority_intact()


def test_executor_rejects_replaced_coordinator_review_root_before_effect(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    runtime = _Runtime()
    runtime.working_directory = str(tmp_path)
    executor = ParallelACExecutor(
        adapter=runtime,  # type: ignore[arg-type]
        event_store=AsyncMock(),
        console=MagicMock(),
        task_cwd=str(tmp_path),
    )
    executor._require_execution_authority_intact()

    async def replacement_review(**_: object) -> object:
        return object()

    monkeypatch.setattr(executor._coordinator, "run_review", replacement_review)

    with pytest.raises(ValueError, match="execution authority drifted"):
        executor._require_execution_authority_intact()


def test_executor_rejects_coordinator_adapter_drift_before_effect(tmp_path) -> None:
    runtime = _Runtime()
    runtime.working_directory = str(tmp_path)
    executor = ParallelACExecutor(
        adapter=runtime,  # type: ignore[arg-type]
        event_store=AsyncMock(),
        console=MagicMock(),
        task_cwd=str(tmp_path),
    )
    executor._require_execution_authority_intact()

    replacement = _Runtime(profile="replacement")
    replacement.working_directory = str(tmp_path)
    executor._coordinator._adapter = replacement  # type: ignore[assignment]

    with pytest.raises(ValueError, match="execution authority drifted"):
        executor._require_execution_authority_intact()


@pytest.mark.asyncio
async def test_executor_rejects_its_own_attribute_resolution_drift_before_dispatch(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    class EvilRuntime(_Runtime):
        def __init__(self) -> None:
            super().__init__()
            self.dispatched = False

        async def execute_task(self, **_: object) -> AsyncIterator[object]:
            self.dispatched = True
            if False:  # pragma: no cover - must remain unreachable
                yield None

    runtime = _Runtime()
    runtime.working_directory = str(tmp_path)
    executor = ParallelACExecutor(
        adapter=runtime,  # type: ignore[arg-type]
        event_store=AsyncMock(),
        console=MagicMock(),
        task_cwd=str(tmp_path),
    )
    evil_runtime = EvilRuntime()
    evil_runtime.working_directory = str(tmp_path)
    original_getattribute = ParallelACExecutor.__getattribute__

    def redirected_getattribute(self: object, name: str) -> object:
        if name == "_require_execution_authority_intact":
            return lambda: None
        if name == "_adapter":
            return evil_runtime
        return original_getattribute(self, name)

    monkeypatch.setattr(
        ParallelACExecutor,
        "__getattribute__",
        redirected_getattribute,
    )

    with pytest.raises(ValueError, match="execution authority drifted"):
        await executor._dispatch_decomposition_prompt(
            prompt="classify this failure",
            system_prompt="Be conservative.",
        )

    assert evil_runtime.dispatched is False


def test_executor_rejects_postconstruction_runtime_attribute_resolution_drift(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    runtime = _Runtime()
    runtime.working_directory = str(tmp_path)
    executor = ParallelACExecutor(
        adapter=runtime,  # type: ignore[arg-type]
        event_store=AsyncMock(),
        console=MagicMock(),
        task_cwd=str(tmp_path),
    )
    assert executor.execution_authority.portable_across_processes is False

    original_getattribute = _Runtime.__getattribute__

    async def injected_dispatch(**_: object) -> AsyncIterator[object]:
        if False:  # pragma: no cover - must remain unreachable
            yield None

    def redirected_getattribute(self: object, name: str) -> object:
        if name == "execute_task":
            return injected_dispatch
        return original_getattribute(self, name)

    monkeypatch.setattr(_Runtime, "__getattribute__", redirected_getattribute)

    with pytest.raises(ValueError, match="execution authority drifted"):
        executor._require_execution_authority_intact()


def test_executor_rejects_postconstruction_dispatcher_attribute_resolution_drift(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    runtime = _Runtime()
    runtime.working_directory = str(tmp_path)
    executor = ParallelACExecutor(
        adapter=runtime,  # type: ignore[arg-type]
        event_store=AsyncMock(),
        console=MagicMock(),
        task_cwd=str(tmp_path),
    )
    assert executor.execution_authority.portable_across_processes is False

    dispatcher_type = executor._authority_leaf_dispatcher_type
    original_getattribute = dispatcher_type.__getattribute__

    async def injected_stream(**_: object) -> None:
        return None

    def redirected_getattribute(self: object, name: str) -> object:
        if name == "stream":
            return injected_stream
        return original_getattribute(self, name)

    monkeypatch.setattr(dispatcher_type, "__getattribute__", redirected_getattribute)

    with pytest.raises(ValueError, match="execution authority drifted"):
        executor._require_execution_authority_intact()


def test_executor_rejects_postconstruction_coordinator_attribute_resolution_drift(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    runtime = _Runtime()
    runtime.working_directory = str(tmp_path)
    executor = ParallelACExecutor(
        adapter=runtime,  # type: ignore[arg-type]
        event_store=AsyncMock(),
        console=MagicMock(),
        task_cwd=str(tmp_path),
    )
    assert executor.execution_authority.portable_across_processes is False

    coordinator_type = type(executor._coordinator)
    original_getattribute = coordinator_type.__getattribute__
    replacement_adapter = object()

    def redirected_getattribute(self: object, name: str) -> object:
        if name == "_adapter":
            return replacement_adapter
        return original_getattribute(self, name)

    monkeypatch.setattr(coordinator_type, "__getattribute__", redirected_getattribute)

    with pytest.raises(ValueError, match="execution authority drifted"):
        executor._require_execution_authority_intact()


@pytest.mark.asyncio
async def test_executor_rejects_rate_gate_attribute_resolution_drift_before_admission(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    runtime = _Runtime()
    runtime.working_directory = str(tmp_path)
    executor = ParallelACExecutor(
        adapter=runtime,  # type: ignore[arg-type]
        event_store=AsyncMock(),
        console=MagicMock(),
        task_cwd=str(tmp_path),
    )
    assert executor.execution_authority.portable_across_processes is False

    gate_type = type(executor._dispatch_rate_gate)
    original_getattribute = gate_type.__getattribute__
    injected_calls: list[int] = []

    async def injected_acquire(*_: object, **__: object) -> None:
        injected_calls.append(1)

    def redirected_getattribute(self: object, name: str) -> object:
        if name == "acquire":
            return injected_acquire
        return original_getattribute(self, name)

    monkeypatch.setattr(gate_type, "__getattribute__", redirected_getattribute)

    with pytest.raises(ValueError, match="execution authority drifted"):
        await executor._await_dispatch_rate_budget(prompt="test", system_prompt=None)

    assert injected_calls == []


@pytest.mark.asyncio
async def test_executor_rejects_replaced_rate_gate_bucket_before_admission(tmp_path) -> None:
    class EvilBucket:
        def __init__(self, original: object) -> None:
            self._runtime_backend = object.__getattribute__(original, "_runtime_backend")
            self._request_limit = object.__getattribute__(original, "_request_limit")
            self._token_limit = object.__getattribute__(original, "_token_limit")
            self._window_seconds = object.__getattribute__(original, "_window_seconds")
            self.called = False

        async def acquire(self, _: int) -> object:
            self.called = True
            return (0.0, object())

    runtime = _Runtime()
    runtime.working_directory = str(tmp_path)
    executor = ParallelACExecutor(
        adapter=runtime,  # type: ignore[arg-type]
        event_store=AsyncMock(),
        console=MagicMock(),
        task_cwd=str(tmp_path),
    )
    original_bucket = object.__getattribute__(executor._dispatch_rate_gate, "_bucket")
    evil_bucket = EvilBucket(original_bucket)
    object.__setattr__(executor._dispatch_rate_gate, "_bucket", evil_bucket)

    with pytest.raises(ValueError, match="execution authority drifted"):
        await executor._await_dispatch_rate_budget(prompt="test", system_prompt=None)

    assert evil_bucket.called is False


@pytest.mark.parametrize(
    ("attribute", "replacement"),
    (("_max_wait_seconds", 0.0), ("_heartbeat_seconds", 0.0)),
)
def test_executor_rejects_rate_gate_scalar_semantic_drift(
    tmp_path,
    attribute: str,
    replacement: float,
) -> None:
    runtime = _Runtime()
    runtime.working_directory = str(tmp_path)
    executor = ParallelACExecutor(
        adapter=runtime,  # type: ignore[arg-type]
        event_store=AsyncMock(),
        console=MagicMock(),
        task_cwd=str(tmp_path),
    )

    object.__setattr__(executor._dispatch_rate_gate, attribute, replacement)

    with pytest.raises(ValueError, match="execution authority drifted"):
        executor._require_execution_authority_intact()


def test_executor_rejects_rate_gate_wait_collaborator_drift(tmp_path) -> None:
    runtime = _Runtime()
    runtime.working_directory = str(tmp_path)
    executor = ParallelACExecutor(
        adapter=runtime,  # type: ignore[arg-type]
        event_store=AsyncMock(),
        console=MagicMock(),
        task_cwd=str(tmp_path),
    )

    async def injected_sleep(_: float) -> None:
        return None

    object.__setattr__(executor._dispatch_rate_gate, "_sleep", injected_sleep)

    with pytest.raises(ValueError, match="execution authority drifted"):
        executor._require_execution_authority_intact()


def test_executor_rejects_rate_gate_bucket_time_and_method_drift(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    runtime = _Runtime()
    runtime.working_directory = str(tmp_path)
    executor = ParallelACExecutor(
        adapter=runtime,  # type: ignore[arg-type]
        event_store=AsyncMock(),
        console=MagicMock(),
        task_cwd=str(tmp_path),
    )
    bucket = executor._dispatch_rate_gate._bucket

    object.__setattr__(bucket, "_time", lambda: 0.0)
    with pytest.raises(ValueError, match="execution authority drifted"):
        executor._require_execution_authority_intact()

    object.__setattr__(bucket, "_time", parallel_executor_module.time.monotonic)

    async def injected_force_reserve(*_: object, **__: object) -> object:
        return object()

    monkeypatch.setattr(type(bucket), "force_reserve", injected_force_reserve)
    with pytest.raises(ValueError, match="execution authority drifted"):
        executor._require_execution_authority_intact()


def test_executor_rejects_rate_gate_bucket_helper_code_drift(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    runtime = _Runtime()
    runtime.working_directory = str(tmp_path)
    executor = ParallelACExecutor(
        adapter=runtime,  # type: ignore[arg-type]
        event_store=AsyncMock(),
        console=MagicMock(),
        task_cwd=str(tmp_path),
    )

    def injected_prune(self: object, now: float) -> None:
        del self, now

    bucket_type = type(executor._dispatch_rate_gate._bucket)
    monkeypatch.setattr(bucket_type._prune, "__code__", injected_prune.__code__)

    with pytest.raises(ValueError, match="execution authority drifted"):
        executor._require_execution_authority_intact()


def test_executor_rejects_replaced_captured_leaf_dispatch_root(tmp_path) -> None:
    runtime = _Runtime()
    runtime.working_directory = str(tmp_path)
    executor = ParallelACExecutor(
        adapter=runtime,  # type: ignore[arg-type]
        event_store=AsyncMock(),
        console=MagicMock(),
        task_cwd=str(tmp_path),
    )

    async def injected_stream(*_: object, **__: object) -> None:
        return None

    executor._authority_leaf_dispatcher_stream = injected_stream

    with pytest.raises(ValueError, match="execution authority drifted"):
        executor._require_execution_authority_intact()


def test_executor_rejects_replaced_captured_rate_gate_root(tmp_path) -> None:
    runtime = _Runtime()
    runtime.working_directory = str(tmp_path)
    executor = ParallelACExecutor(
        adapter=runtime,  # type: ignore[arg-type]
        event_store=AsyncMock(),
        console=MagicMock(),
        task_cwd=str(tmp_path),
    )

    async def injected_acquire(*_: object, **__: object) -> None:
        return None

    executor._authority_rate_gate_acquire_root = injected_acquire

    with pytest.raises(ValueError, match="execution authority drifted"):
        executor._require_execution_authority_intact()


def test_executor_reuses_closed_leaf_dispatcher_after_late_init_patch(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    runtime = _Runtime()
    runtime.working_directory = str(tmp_path)
    executor = ParallelACExecutor(
        adapter=runtime,  # type: ignore[arg-type]
        event_store=AsyncMock(),
        console=MagicMock(),
        task_cwd=str(tmp_path),
    )
    captured_dispatcher = executor._authority_leaf_dispatcher

    def injected_init(self: object, _: object) -> None:
        object.__setattr__(self, "_executor", object())

    monkeypatch.setattr(executor._authority_leaf_dispatcher_type, "__init__", injected_init)

    executor._require_execution_authority_intact()
    assert executor._authority_leaf_dispatcher is captured_dispatcher
    assert object.__getattribute__(captured_dispatcher, "_executor") is executor


def test_executor_uses_closed_import_time_dispatcher_root(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    class ReplacementDispatcher:
        pass

    monkeypatch.setattr(parallel_executor_module, "LeafDispatcher", ReplacementDispatcher)
    runtime = _Runtime()
    runtime.working_directory = str(tmp_path)

    executor = ParallelACExecutor(
        adapter=runtime,  # type: ignore[arg-type]
        event_store=AsyncMock(),
        console=MagicMock(),
        task_cwd=str(tmp_path),
    )

    assert executor._authority_leaf_dispatcher_type is not ReplacementDispatcher
    executor._require_execution_authority_intact()


def test_executor_uses_closed_import_time_coordinator_root(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    class ReplacementCoordinator:
        pass

    monkeypatch.setattr(parallel_executor_module, "LevelCoordinator", ReplacementCoordinator)
    runtime = _Runtime()
    runtime.working_directory = str(tmp_path)

    executor = ParallelACExecutor(
        adapter=runtime,  # type: ignore[arg-type]
        event_store=AsyncMock(),
        console=MagicMock(),
        task_cwd=str(tmp_path),
    )

    assert type(executor._coordinator) is not ReplacementCoordinator
    executor._require_execution_authority_intact()


def test_preconstruction_dispatcher_member_patch_is_process_local(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    async def replacement_stream(self: object, **_: object) -> None:
        del self

    monkeypatch.setattr(
        parallel_executor_module._FOUNDATION_A_CLOSED_ROOTS.leaf_dispatcher_type,
        "stream",
        replacement_stream,
    )
    runtime = _Runtime()
    runtime.working_directory = str(tmp_path)
    executor = ParallelACExecutor(
        adapter=runtime,  # type: ignore[arg-type]
        event_store=AsyncMock(),
        console=MagicMock(),
        task_cwd=str(tmp_path),
    )

    assert executor.execution_authority.portable_across_processes is False
    executor._require_execution_authority_intact()


def test_preconstruction_rate_gate_member_patch_is_process_local(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    async def replacement_acquire(self: object, *_: object, **__: object) -> None:
        del self

    monkeypatch.setattr(
        parallel_executor_module.RateLimitGate,
        "acquire",
        replacement_acquire,
    )
    runtime = _Runtime()
    runtime.working_directory = str(tmp_path)
    executor = ParallelACExecutor(
        adapter=runtime,  # type: ignore[arg-type]
        event_store=AsyncMock(),
        console=MagicMock(),
        task_cwd=str(tmp_path),
    )

    assert executor.execution_authority.portable_across_processes is False
    executor._require_execution_authority_intact()


def test_preconstruction_coordinator_member_patch_is_process_local(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    async def replacement_review(self: object, **_: object) -> object:
        del self
        return object()

    monkeypatch.setattr(
        parallel_executor_module._FOUNDATION_A_CLOSED_ROOTS.level_coordinator_type,
        "run_review",
        replacement_review,
    )
    runtime = _Runtime()
    runtime.working_directory = str(tmp_path)
    executor = ParallelACExecutor(
        adapter=runtime,  # type: ignore[arg-type]
        event_store=AsyncMock(),
        console=MagicMock(),
        task_cwd=str(tmp_path),
    )

    assert executor.execution_authority.portable_across_processes is False
    executor._require_execution_authority_intact()


def test_preconstruction_transcript_alias_patch_uses_closed_root(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    def injected_verifier(**_: object) -> VerifierVerdict:
        return VerifierVerdict(
            passed=False,
            reasons=("injected verifier must not become the closed root",),
        )

    monkeypatch.setattr(
        parallel_executor_module,
        "_FOUNDATION_A_TRANSCRIPT_VERIFIER",
        injected_verifier,
    )
    runtime = _Runtime()
    runtime.working_directory = str(tmp_path)
    executor = ParallelACExecutor(
        adapter=runtime,  # type: ignore[arg-type]
        event_store=AsyncMock(),
        console=MagicMock(),
        task_cwd=str(tmp_path),
    )

    assert executor._authority_transcript_verifier is not injected_verifier
    assert executor.execution_authority.portable_across_processes is False
    executor._require_execution_authority_intact()


def test_reflective_custom_verifier_remains_process_local_without_graph_introspection() -> None:
    mutable_state = {"passed": True}

    def verifier(**_: object) -> VerifierVerdict:
        return VerifierVerdict(passed=mutable_state["passed"])

    authority = _contract(verifier=verifier)

    assert authority.portable_across_processes is False
    assert "mutable_state" not in authority.canonical_json
    assert "passed" not in authority.canonical_json


def test_uninspectable_runtime_dispatch_root_stays_process_local() -> None:
    class Dispatcher:
        async def stream(self, **_: object) -> None:
            return None

    class RateGate:
        async def acquire(self, *_: object, **__: object) -> None:
            return None

    def transcript_verifier(**_: object) -> VerifierVerdict:
        return VerifierVerdict(passed=True)

    from ouroboros.orchestrator.execution_authority import ExecutionAuthorityLiveBinding

    binding = ExecutionAuthorityLiveBinding.capture(
        adapter=_DynamicDispatchRuntime(),
        verifier=None,
        dispatcher_type=Dispatcher,
        transcript_verifier=transcript_verifier,
        rate_gate=RateGate(),
        workspace="/tmp/workspace-a",
        execution_policy={"retry_attempts": 2},
    )

    assert binding.adapter_dispatch_root is None
    assert binding.contract.portable_across_processes is False
    assert binding.contract.data["runtime"]["stability"] == "process_local"


def test_instance_level_runtime_dispatch_callable_stays_process_local() -> None:
    class InstanceDispatchRuntime(_Runtime):
        def __init__(self, dispatch: object) -> None:
            super().__init__()
            self.execute_task = dispatch  # type: ignore[method-assign]

    async def first_dispatch(**_: object) -> AsyncIterator[object]:
        if False:  # pragma: no cover - identity-only stand-in
            yield None

    async def second_dispatch(**_: object) -> AsyncIterator[object]:
        if False:  # pragma: no cover - identity-only stand-in
            yield None

    first = _contract(runtime=InstanceDispatchRuntime(first_dispatch))
    second = _contract(runtime=InstanceDispatchRuntime(second_dispatch))

    assert first.portable_across_processes is False
    assert second.portable_across_processes is False
    assert first.data["runtime"]["stability"] == "process_local"
    assert second.data["runtime"]["stability"] == "process_local"


def test_direct_contract_with_uninspectable_runtime_stays_process_local() -> None:
    authority = ExecutionAuthorityContract.build(
        adapter=_DynamicDispatchRuntime(),
        verifier=None,
        workspace="/tmp/workspace-a",
        execution_policy={"retry_attempts": 2},
    )

    assert authority.portable_across_processes is False
    assert authority.data["runtime"]["stability"] == "process_local"


def test_captured_transcript_verifier_ignores_replaced_executor_wrapper(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    runtime = _Runtime()
    runtime.working_directory = str(tmp_path)
    profile = load_profile("code").model_copy(
        update={"evidence_schema": EvidenceSchema(required=())}
    )
    executor = ParallelACExecutor(
        adapter=runtime,  # type: ignore[arg-type]
        event_store=AsyncMock(),
        console=MagicMock(),
        task_cwd=str(tmp_path),
        execution_profile=profile,
        fat_harness_mode=True,
    )

    def replaced_wrapper(**_: object) -> VerifierVerdict:
        return VerifierVerdict(
            passed=False,
            reasons=("replacement wrapper must not decide acceptance",),
        )

    monkeypatch.setattr(
        ParallelACExecutor,
        "_verify_atomic_evidence_against_runtime_messages",
        replaced_wrapper,
    )

    verdict = executor._run_atomic_verifier_pass(
        ac_content="No transcript proof is required.",
        final_message="done",
        success=True,
        messages=(),
        typed_evidence=parallel_executor_module.EvidenceRecord(data={}),
        typed_validation=parallel_executor_module.ValidationResult(ok=True),
    )

    assert verdict is not None
    assert verdict.passed is True


def test_executor_rejects_in_place_dispatcher_code_drift(tmp_path) -> None:
    runtime = _Runtime()
    runtime.working_directory = str(tmp_path)
    executor = ParallelACExecutor(
        adapter=runtime,  # type: ignore[arg-type]
        event_store=AsyncMock(),
        console=MagicMock(),
        task_cwd=str(tmp_path),
    )
    dispatcher_type = executor._authority_leaf_dispatcher_type
    original_code = dispatcher_type.stream.__code__

    async def injected_stream(self: object, **_: object) -> None:
        del self

    dispatcher_type.stream.__code__ = injected_stream.__code__
    try:
        with pytest.raises(ValueError, match="execution authority drifted"):
            executor._require_execution_authority_intact()
    finally:
        dispatcher_type.stream.__code__ = original_code


def test_executor_rejects_in_place_structural_verifier_code_drift(tmp_path) -> None:
    runtime = _Runtime()
    runtime.working_directory = str(tmp_path)
    executor = ParallelACExecutor(
        adapter=runtime,  # type: ignore[arg-type]
        event_store=AsyncMock(),
        console=MagicMock(),
        task_cwd=str(tmp_path),
        atomic_verifier=structural_atomic_verifier,
    )
    original_code = structural_atomic_verifier.__code__

    def injected_structural_verifier(
        *,
        profile: object,
        ac: str,
        leaf_output: str,
        record: object,
    ) -> VerifierVerdict:
        del profile, ac, leaf_output, record
        return VerifierVerdict(passed=True)

    structural_atomic_verifier.__code__ = injected_structural_verifier.__code__
    try:
        with pytest.raises(ValueError, match="execution authority drifted"):
            executor._require_execution_authority_intact()
    finally:
        structural_atomic_verifier.__code__ = original_code


def test_preconstruction_structural_verifier_code_drift_is_process_local() -> None:
    original_code = structural_atomic_verifier.__code__

    def injected_structural_verifier(
        *,
        profile: object,
        ac: str,
        leaf_output: str,
        record: object,
    ) -> VerifierVerdict:
        del profile, ac, leaf_output, record
        return VerifierVerdict(passed=True)

    structural_atomic_verifier.__code__ = injected_structural_verifier.__code__
    try:
        authority = _contract(verifier=structural_atomic_verifier)
        assert authority.portable_across_processes is False
        assert authority.data["verifier"]["stability"] == "process_local"

        runtime = _Runtime()
        executor = ParallelACExecutor(
            adapter=runtime,  # type: ignore[arg-type]
            event_store=AsyncMock(),
            console=MagicMock(),
            task_cwd="/tmp/workspace-a",
            atomic_verifier=structural_atomic_verifier,
        )
        assert executor.execution_authority.portable_across_processes is False
        executor._require_execution_authority_intact()
    finally:
        structural_atomic_verifier.__code__ = original_code


@pytest.mark.parametrize(
    "entry_name",
    (
        "_execute_single_ac",
        "_execute_atomic_ac",
        "_await_dispatch_rate_budget",
        "_dispatch_decomposition_prompt",
        "_run_atomic_verifier_pass",
        "_run_ac_verify_gate",
    ),
)
def test_executor_rejects_postconstruction_internal_entry_root_drift(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
    entry_name: str,
) -> None:
    runtime = _Runtime()
    runtime.working_directory = str(tmp_path)
    executor = ParallelACExecutor(
        adapter=runtime,  # type: ignore[arg-type]
        event_store=AsyncMock(),
        console=MagicMock(),
        task_cwd=str(tmp_path),
    )

    async def injected_entry(*_: object, **__: object) -> object:
        return None

    monkeypatch.setattr(ParallelACExecutor, entry_name, injected_entry)

    with pytest.raises(ValueError, match="execution authority drifted"):
        executor._require_execution_authority_intact()


def test_executor_rejects_instance_shadow_of_internal_entry_root(tmp_path) -> None:
    runtime = _Runtime()
    runtime.working_directory = str(tmp_path)
    executor = ParallelACExecutor(
        adapter=runtime,  # type: ignore[arg-type]
        event_store=AsyncMock(),
        console=MagicMock(),
        task_cwd=str(tmp_path),
    )

    async def injected_entry(**_: object) -> object:
        return None

    object.__setattr__(executor, "_execute_single_ac", injected_entry)

    with pytest.raises(ValueError, match="execution authority drifted"):
        executor._require_execution_authority_intact()


def test_preconstruction_internal_entry_root_patch_is_process_local(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    async def injected_entry(*_: object, **__: object) -> object:
        return None

    monkeypatch.setattr(ParallelACExecutor, "_dispatch_decomposition_prompt", injected_entry)
    runtime = _Runtime()
    runtime.working_directory = str(tmp_path)
    executor = ParallelACExecutor(
        adapter=runtime,  # type: ignore[arg-type]
        event_store=AsyncMock(),
        console=MagicMock(),
        task_cwd=str(tmp_path),
    )

    assert executor.execution_authority.portable_across_processes is False
    executor._require_execution_authority_intact()


def test_executor_subclass_is_process_local_even_when_it_inherits_entry_roots(tmp_path) -> None:
    class SubclassedExecutor(ParallelACExecutor):
        pass

    runtime = _Runtime()
    runtime.working_directory = str(tmp_path)
    executor = SubclassedExecutor(
        adapter=runtime,  # type: ignore[arg-type]
        event_store=AsyncMock(),
        console=MagicMock(),
        task_cwd=str(tmp_path),
    )

    assert executor.execution_authority.portable_across_processes is False


def test_unhashable_executor_subclass_remains_executable_and_process_local(tmp_path) -> None:
    class UnhashableExecutor(ParallelACExecutor):
        __hash__ = None  # type: ignore[assignment]

    runtime = _Runtime()
    runtime.working_directory = str(tmp_path)
    executor = UnhashableExecutor(
        adapter=runtime,  # type: ignore[arg-type]
        event_store=AsyncMock(),
        console=MagicMock(),
        task_cwd=str(tmp_path),
    )

    assert executor.execution_authority.portable_across_processes is False
    executor._require_execution_authority_intact()


def test_equality_overriding_executor_subclasses_keep_distinct_registry_entries(tmp_path) -> None:
    class EqualExecutor(ParallelACExecutor):
        def __eq__(self, other: object) -> bool:
            return isinstance(other, EqualExecutor)

        __hash__ = object.__hash__

    first_runtime = _Runtime(profile="first")
    first_runtime.working_directory = str(tmp_path)
    second_runtime = _Runtime(profile="second")
    second_runtime.working_directory = str(tmp_path)
    first = EqualExecutor(
        adapter=first_runtime,  # type: ignore[arg-type]
        event_store=AsyncMock(),
        console=MagicMock(),
        task_cwd=str(tmp_path),
    )
    second = EqualExecutor(
        adapter=second_runtime,  # type: ignore[arg-type]
        event_store=AsyncMock(),
        console=MagicMock(),
        task_cwd=str(tmp_path),
    )

    assert first == second
    first._require_execution_authority_intact()
    second._require_execution_authority_intact()


@pytest.mark.asyncio
async def test_dynamic_runtime_does_not_reopen_captured_executor_entry_roots(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    runtime = _DynamicDispatchRuntime()
    runtime.working_directory = str(tmp_path)
    executor = ParallelACExecutor(
        adapter=runtime,  # type: ignore[arg-type]
        event_store=AsyncMock(),
        console=MagicMock(),
        task_cwd=str(tmp_path),
    )
    assert executor.execution_authority.portable_across_processes is False
    injected = False

    async def injected_entry(*_: object, **__: object) -> str:
        nonlocal injected
        injected = True
        return '{"cause":"TOO_BIG","reason":"injected","evidence_refs":[]}'

    monkeypatch.setattr(ParallelACExecutor, "_dispatch_decomposition_prompt", injected_entry)

    result = await executor._request_bounce_classification(
        trace=parallel_executor_module.DecompositionTraceSummary(summary="bounded evidence"),
    )

    assert result == (
        parallel_executor_module.BounceCause.UNKNOWN,
        "Bounce classifier returned no admissible cause.",
        (),
        False,
    )
    assert injected is False


@pytest.mark.asyncio
async def test_internal_execution_path_rejects_entry_replacement_before_dispatch(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    class DispatchRuntime(_Runtime):
        def __init__(self) -> None:
            super().__init__()
            self.dispatched = False

        async def execute_task(self, **_: object) -> AsyncIterator[object]:
            self.dispatched = True
            if False:  # pragma: no cover - must remain unreachable
                yield None

    runtime = DispatchRuntime()
    runtime.working_directory = str(tmp_path)
    executor = ParallelACExecutor(
        adapter=runtime,  # type: ignore[arg-type]
        event_store=AsyncMock(),
        console=MagicMock(),
        task_cwd=str(tmp_path),
    )

    async def injected_entry(*_: object, **__: object) -> object:
        runtime.dispatched = True
        return None

    monkeypatch.setattr(ParallelACExecutor, "_execute_single_ac", injected_entry)
    seed = MagicMock()
    seed.acceptance_criteria = ("Do not dispatch",)
    seed.goal = "Keep the runtime closed"

    results = await executor._execute_ac_batch(
        seed=seed,
        batch_indices=[0],
        session_id="session",
        execution_id="execution",
        tools=[],
        tool_catalog=None,
        system_prompt="system",
        level_contexts=[],
        ac_retry_attempts={0: 0},
    )

    assert isinstance(results[0], ValueError)
    assert runtime.dispatched is False


@pytest.mark.asyncio
async def test_executor_rejects_authority_drift_before_decomposition_dispatch(tmp_path) -> None:
    class DispatchRuntime(_Runtime):
        def __init__(self) -> None:
            super().__init__()
            self.dispatched = False

        async def execute_task(self, **_: object) -> AsyncIterator[object]:
            self.dispatched = True
            if False:  # pragma: no cover - should remain unreachable
                yield None

    runtime = DispatchRuntime()
    runtime.working_directory = str(tmp_path)
    executor = ParallelACExecutor(
        adapter=runtime,  # type: ignore[arg-type]
        event_store=AsyncMock(),
        console=MagicMock(),
        task_cwd=str(tmp_path),
    )
    runtime.profile = "drifted"

    with pytest.raises(ValueError, match="execution authority drifted"):
        await executor._dispatch_decomposition_prompt(
            prompt="classify this failure",
            system_prompt="Be conservative.",
        )

    assert runtime.dispatched is False
