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

    def test_read_only_tool_set(self, code_profile: ExecutionProfile) -> None:
        route = decide_route(role=DispatchRole.VERIFIER, profile=code_profile)
        assert set(route.tools) == {"Read", "Glob", "Grep"}
        # Mutating tools must not appear.
        assert "Edit" not in route.tools
        assert "Write" not in route.tools
        assert "Bash" not in route.tools


class TestRationaleStrings:
    def test_each_route_has_rationale(self, code_profile: ExecutionProfile) -> None:
        for role in DispatchRole:
            route = decide_route(role=role, profile=code_profile)
            assert route.rationale, f"{role} returned empty rationale"
