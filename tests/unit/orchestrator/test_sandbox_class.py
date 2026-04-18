"""Cross-backend invariants for the engine ``SandboxClass`` vocabulary."""

from __future__ import annotations

import pytest

from ouroboros.claude_permissions import (
    _SANDBOX_TO_CLAUDE_MODE,
    claude_permission_mode_for_sandbox,
)
from ouroboros.codex_permissions import (
    _SANDBOX_TO_CODEX_ARGS,
    build_codex_exec_args_for_sandbox,
    build_codex_exec_permission_args,
)
from ouroboros.orchestrator.policy import (
    PolicyContext,
    PolicyExecutionPhase,
    PolicySessionRole,
    SandboxClass,
    derive_sandbox_class,
)


class TestDeriveSandboxClass:
    """Every admitted session role resolves to exactly one sandbox class."""

    def test_interview_and_evaluation_are_read_only(self) -> None:
        for role, phase in (
            (PolicySessionRole.INTERVIEW, PolicyExecutionPhase.INTERVIEW),
            (PolicySessionRole.EVALUATION, PolicyExecutionPhase.EVALUATION),
        ):
            ctx = PolicyContext(
                runtime_backend="opencode",
                session_role=role,
                execution_phase=phase,
            )
            assert derive_sandbox_class(ctx) is SandboxClass.READ_ONLY

    def test_coordinator_is_workspace_write(self) -> None:
        ctx = PolicyContext(
            runtime_backend="opencode",
            session_role=PolicySessionRole.COORDINATOR,
            execution_phase=PolicyExecutionPhase.COORDINATOR_REVIEW,
        )
        assert derive_sandbox_class(ctx) is SandboxClass.WORKSPACE_WRITE

    def test_implementation_is_unrestricted(self) -> None:
        ctx = PolicyContext(
            runtime_backend="codex",
            session_role=PolicySessionRole.IMPLEMENTATION,
            execution_phase=PolicyExecutionPhase.IMPLEMENTATION,
        )
        assert derive_sandbox_class(ctx) is SandboxClass.UNRESTRICTED

    def test_every_role_maps_to_a_sandbox_class(self) -> None:
        """Adding a role must also add a sandbox mapping — no silent defaults."""
        role_phase_pairs = {
            PolicySessionRole.IMPLEMENTATION: PolicyExecutionPhase.IMPLEMENTATION,
            PolicySessionRole.COORDINATOR: PolicyExecutionPhase.COORDINATOR_REVIEW,
            PolicySessionRole.INTERVIEW: PolicyExecutionPhase.INTERVIEW,
            PolicySessionRole.EVALUATION: PolicyExecutionPhase.EVALUATION,
        }
        assert set(role_phase_pairs) == set(PolicySessionRole), (
            "New PolicySessionRole must be covered here"
        )
        for role, phase in role_phase_pairs.items():
            ctx = PolicyContext(
                runtime_backend="codex",
                session_role=role,
                execution_phase=phase,
            )
            # Must not raise KeyError.
            derive_sandbox_class(ctx)


class TestBackendMappingCompleteness:
    """Every SandboxClass value must have a mapping in every provider table.

    This is the invariant that makes the layering honest: without it, a new
    sandbox class could ship with a matching Codex entry but a missing Claude
    entry, silently re-creating the drift this refactor exists to eliminate.
    """

    def test_codex_table_covers_every_sandbox_class(self) -> None:
        for sandbox in SandboxClass:
            # Does not raise; returns a non-empty list.
            args = build_codex_exec_args_for_sandbox(sandbox)
            assert args, f"Codex mapping for {sandbox!r} is empty"
        assert set(_SANDBOX_TO_CODEX_ARGS) == set(SandboxClass)

    def test_claude_table_covers_every_sandbox_class(self) -> None:
        for sandbox in SandboxClass:
            mode = claude_permission_mode_for_sandbox(sandbox)
            assert mode, f"Claude mapping for {sandbox!r} is empty"
        assert set(_SANDBOX_TO_CLAUDE_MODE) == set(SandboxClass)


class TestCodexLegacyWrapperPreservesBehavior:
    """``build_codex_exec_permission_args`` must produce the same flags as
    the new SandboxClass entry point for every supported mode.  Otherwise
    the two call paths could drift and a caller on the legacy string path
    would get different Codex flags than a caller on the new enum path.
    """

    @pytest.mark.parametrize(
        ("mode", "expected_sandbox"),
        [
            ("default", SandboxClass.READ_ONLY),
            ("acceptEdits", SandboxClass.WORKSPACE_WRITE),
            ("bypassPermissions", SandboxClass.UNRESTRICTED),
        ],
    )
    def test_legacy_wrapper_routes_through_sandbox_enum(
        self, mode: str, expected_sandbox: SandboxClass
    ) -> None:
        legacy_args = build_codex_exec_permission_args(mode)
        enum_args = build_codex_exec_args_for_sandbox(expected_sandbox)
        assert legacy_args == enum_args


class TestClaudeLegacyVocabularyMatchesSandbox:
    """Claude SDK historically consumed the same ``default``/``acceptEdits``/
    ``bypassPermissions`` strings we now treat as engine sandbox values.
    This test pins that the enum-driven mapping still produces exactly those
    strings, so the Claude adapter can adopt the enum without a behavior
    change visible to the SDK.
    """

    def test_read_only_renders_as_default(self) -> None:
        assert claude_permission_mode_for_sandbox(SandboxClass.READ_ONLY) == "default"

    def test_workspace_write_renders_as_accept_edits(self) -> None:
        assert claude_permission_mode_for_sandbox(SandboxClass.WORKSPACE_WRITE) == "acceptEdits"

    def test_unrestricted_renders_as_bypass_permissions(self) -> None:
        assert claude_permission_mode_for_sandbox(SandboxClass.UNRESTRICTED) == "bypassPermissions"
