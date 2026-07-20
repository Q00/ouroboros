from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from ouroboros.orchestrator.adapter import FULL_CAPABILITIES
from ouroboros.orchestrator.execution_authority import ExecutionAuthorityContract
from ouroboros.orchestrator.parallel_executor import ParallelACExecutor
from ouroboros.orchestrator.profile_loader import ExecutionProfile, load_profile
from ouroboros.orchestrator.verifier import VerifierVerdict


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


class _Verifier:
    def __init__(self, identity: str) -> None:
        self.identity = identity

    def verification_identity_contract(self) -> dict[str, object]:
        return {"judge": self.identity}

    def __call__(self, **_: object) -> VerifierVerdict:
        return VerifierVerdict(passed=True)


def _contract(
    *,
    runtime: _Runtime | None = None,
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


def test_verifier_identity_drift_changes_authority() -> None:
    first = _contract(verifier=_Verifier("judge-a"))
    same = _contract(verifier=_Verifier("judge-a"))
    changed = _contract(verifier=_Verifier("judge-b"))

    assert first.fingerprint == same.fingerprint
    assert first.fingerprint != changed.fingerprint
    assert first.reusable_across_processes is True


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
    assert authority.data["workspace"]["effective_cwd"] == str(tmp_path.resolve())
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
