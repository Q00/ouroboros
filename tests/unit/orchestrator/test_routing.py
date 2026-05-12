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
    def test_code_profile_gets_test_runner_tools(self, code_profile: ExecutionProfile) -> None:
        route = decide_route(role=DispatchRole.VERIFIER, profile=code_profile)
        assert route.tier == ModelTier.OPUS
        assert route.tools == ("Read", "Glob", "Grep")
        assert route.requires_external_test_runner is True
        assert "raw Bash is not granted" in route.rationale

    @pytest.mark.parametrize("profile_name", ["research", "analysis"])
    def test_read_only_profiles_exclude_bash(self, profile_name: str) -> None:
        profile = load_profile(profile_name)
        route = decide_route(role=DispatchRole.VERIFIER, profile=profile)
        assert route.tier == ModelTier.OPUS
        assert route.tools == ("Read", "Glob", "Grep")
        assert route.requires_external_test_runner is False
        assert "read-only" in route.rationale

    def test_fabrication_retry_caps_verifier_at_opus(self, code_profile: ExecutionProfile) -> None:
        route = decide_route(
            role=DispatchRole.VERIFIER,
            profile=code_profile,
            fabrication_retry=True,
        )
        assert route.tier == ModelTier.OPUS


class TestRationaleStrings:
    def test_decomposer_and_executor_have_rationale(self, code_profile: ExecutionProfile) -> None:
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
