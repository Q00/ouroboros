"""Tests for ouroboros.orchestrator.routing (RFC v2 #830, PR 7)."""

from __future__ import annotations

import pytest

from ouroboros.orchestrator.profile_loader import ExecutionProfile, load_profile
from ouroboros.orchestrator.routing import (
    DispatchRole,
    ModelTier,
    decide_route,
)


@pytest.fixture
def code_profile() -> ExecutionProfile:
    return load_profile("code")


@pytest.fixture
def research_profile() -> ExecutionProfile:
    return load_profile("research")


class TestDecomposerRoute:
    def test_uses_haiku(self, code_profile: ExecutionProfile) -> None:
        route = decide_route(role=DispatchRole.DECOMPOSER, profile=code_profile)
        assert route.tier == ModelTier.HAIKU

    def test_empty_tool_set(self, code_profile: ExecutionProfile) -> None:
        route = decide_route(role=DispatchRole.DECOMPOSER, profile=code_profile)
        assert route.tools == ()

    def test_decomposer_ignores_fabrication_flag(self, code_profile: ExecutionProfile) -> None:
        plain = decide_route(role=DispatchRole.DECOMPOSER, profile=code_profile)
        retry = decide_route(
            role=DispatchRole.DECOMPOSER,
            profile=code_profile,
            fabrication_retry=True,
        )
        assert plain.tier == retry.tier == ModelTier.HAIKU


class TestExecutorRoute:
    def test_default_is_sonnet(self, code_profile: ExecutionProfile) -> None:
        route = decide_route(role=DispatchRole.EXECUTOR, profile=code_profile)
        assert route.tier == ModelTier.SONNET

    def test_tools_come_from_profile(self, code_profile: ExecutionProfile) -> None:
        route = decide_route(role=DispatchRole.EXECUTOR, profile=code_profile)
        assert route.tools == code_profile.suggested_tools
        assert "Read" in route.tools and "Edit" in route.tools

    def test_research_profile_tools_distinct(self, research_profile: ExecutionProfile) -> None:
        route = decide_route(role=DispatchRole.EXECUTOR, profile=research_profile)
        # Research profile in #881 does not declare Edit.
        assert "Edit" not in route.tools

    def test_fabrication_retry_escalates_to_opus(self, code_profile: ExecutionProfile) -> None:
        route = decide_route(
            role=DispatchRole.EXECUTOR,
            profile=code_profile,
            fabrication_retry=True,
        )
        assert route.tier == ModelTier.OPUS


class TestVerifierRoute:
    def test_default_one_tier_above_executor(self, code_profile: ExecutionProfile) -> None:
        route = decide_route(role=DispatchRole.VERIFIER, profile=code_profile)
        # Default executor = SONNET → verifier = OPUS.
        assert route.tier == ModelTier.OPUS

    def test_fabrication_caps_at_opus(self, code_profile: ExecutionProfile) -> None:
        # Executor escalates to OPUS; verifier "one above" should cap there.
        route = decide_route(
            role=DispatchRole.VERIFIER,
            profile=code_profile,
            fabrication_retry=True,
        )
        assert route.tier == ModelTier.OPUS

    def test_excludes_file_mutation_tools(self, code_profile: ExecutionProfile) -> None:
        # H1 read-only contract is about source files. Edit/Write must
        # be filtered out of the verifier tool set.
        route = decide_route(role=DispatchRole.VERIFIER, profile=code_profile)
        assert "Edit" not in route.tools
        assert "Write" not in route.tools

    def test_keeps_bash_for_code_profile(self, code_profile: ExecutionProfile) -> None:
        # code profile's verifier_focus says "Run the project's test
        # command". The verifier MUST be able to invoke Bash or the
        # routing layer chooses a tool set that cannot satisfy its own
        # verifier contract (bot finding on #889).
        route = decide_route(role=DispatchRole.VERIFIER, profile=code_profile)
        assert "Bash" in route.tools

    def test_verifier_tools_derived_from_profile(self) -> None:
        # Research profile has no Bash; verifier tools should reflect
        # that even though Bash is generally permitted for verifiers.
        from ouroboros.orchestrator.profile_loader import EvidenceSchema

        custom = load_profile("research").model_copy(
            update={
                "suggested_tools": ("Read", "Glob", "Grep"),
                "evidence_schema": EvidenceSchema(),
            }
        )
        route = decide_route(role=DispatchRole.VERIFIER, profile=custom)
        assert "Bash" not in route.tools
        assert set(route.tools) == {"Read", "Glob", "Grep"}


class TestRationaleStrings:
    def test_each_route_has_rationale(self, code_profile: ExecutionProfile) -> None:
        for role in DispatchRole:
            route = decide_route(role=role, profile=code_profile)
            assert route.rationale, f"{role} returned empty rationale"


class TestRoleValidation:
    """Unknown role types must fail fast, not silently route to VERIFIER.

    Without this guard a raw string from config/JSON (e.g. "EXECUTOR")
    would compare with `is` to the enum member, fall through every
    `is` check, and silently end up on the verifier branch — wrong
    model tier, wrong tools, no error.
    """

    def test_raw_string_rejected(self, code_profile: ExecutionProfile) -> None:
        with pytest.raises(TypeError, match="DispatchRole"):
            decide_route(role="EXECUTOR", profile=code_profile)  # type: ignore[arg-type]

    def test_none_rejected(self, code_profile: ExecutionProfile) -> None:
        with pytest.raises(TypeError, match="DispatchRole"):
            decide_route(role=None, profile=code_profile)  # type: ignore[arg-type]

    def test_int_rejected(self, code_profile: ExecutionProfile) -> None:
        with pytest.raises(TypeError, match="DispatchRole"):
            decide_route(role=0, profile=code_profile)  # type: ignore[arg-type]

    def test_error_message_lists_valid_roles(self, code_profile: ExecutionProfile) -> None:
        with pytest.raises(TypeError) as exc:
            decide_route(role="VERIFIER", profile=code_profile)  # type: ignore[arg-type]
        msg = str(exc.value)
        for role in DispatchRole:
            assert role.name in msg
