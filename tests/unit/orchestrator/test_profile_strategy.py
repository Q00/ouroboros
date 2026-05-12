"""Tests for ouroboros.orchestrator.profile_strategy (RFC v2 #830, PR 9)."""

from __future__ import annotations

import pytest

from ouroboros.orchestrator.execution_strategy import ExecutionStrategy
from ouroboros.orchestrator.profile_loader import ExecutionProfile, load_profile
from ouroboros.orchestrator.profile_strategy import ProfileBackedStrategy
from ouroboros.orchestrator.workflow_state import ActivityType


@pytest.fixture(params=["code", "research", "analysis"])
def profile(request: pytest.FixtureRequest) -> ExecutionProfile:
    return load_profile(request.param)


class TestProtocolConformance:
    def test_satisfies_execution_strategy(self, profile: ExecutionProfile) -> None:
        strategy = ProfileBackedStrategy(profile)
        # Runtime-checkable Protocol from execution_strategy.py.
        assert isinstance(strategy, ExecutionStrategy)

    def test_tools_come_from_profile(self, profile: ExecutionProfile) -> None:
        strategy = ProfileBackedStrategy(profile)
        assert strategy.get_tools() == list(profile.suggested_tools)

    def test_get_tools_returns_fresh_list(self, profile: ExecutionProfile) -> None:
        strategy = ProfileBackedStrategy(profile)
        first = strategy.get_tools()
        first.append("Mutated")
        # Mutating the returned list must not bleed into the strategy's
        # view — frozen profile data should stay frozen for callers.
        assert strategy.get_tools() != first


class TestSystemPromptFragment:
    def test_mentions_profile_axis_and_min_unit(self, profile: ExecutionProfile) -> None:
        fragment = ProfileBackedStrategy(profile).get_system_prompt_fragment()
        assert profile.profile in fragment
        assert profile.axis in fragment
        assert profile.min_unit in fragment

    def test_surfaces_verifier_focus(self, profile: ExecutionProfile) -> None:
        fragment = ProfileBackedStrategy(profile).get_system_prompt_fragment()
        # First word of verifier focus must be present so the leaf
        # sees the verifier's expectation before acting.
        first_token = profile.verifier_focus.strip().split()[0]
        assert first_token in fragment

    def test_profiles_produce_distinct_fragments(self) -> None:
        c = ProfileBackedStrategy(load_profile("code")).get_system_prompt_fragment()
        r = ProfileBackedStrategy(load_profile("research")).get_system_prompt_fragment()
        a = ProfileBackedStrategy(load_profile("analysis")).get_system_prompt_fragment()
        assert c != r != a


class TestTaskPromptSuffix:
    def test_forbids_self_declared_done(self, profile: ExecutionProfile) -> None:
        suffix = ProfileBackedStrategy(profile).get_task_prompt_suffix()
        assert "DONE" in suffix
        assert "evidence" in suffix.lower()

    def test_suffix_is_profile_independent(self) -> None:
        # The suffix is structural (H1/H2 hooks), so it should be the
        # same string across profiles.
        c = ProfileBackedStrategy(load_profile("code")).get_task_prompt_suffix()
        r = ProfileBackedStrategy(load_profile("research")).get_task_prompt_suffix()
        assert c == r


class TestActivityMap:
    def test_known_tools_get_canonical_activity(self) -> None:
        strategy = ProfileBackedStrategy(load_profile("code"))
        activity_map = strategy.get_activity_map()
        assert activity_map["Read"] == ActivityType.EXPLORING
        assert activity_map["Edit"] == ActivityType.BUILDING
        assert activity_map["Bash"] == ActivityType.TESTING

    def test_only_profile_tools_appear(self, profile: ExecutionProfile) -> None:
        strategy = ProfileBackedStrategy(profile)
        activity_map = strategy.get_activity_map()
        assert set(activity_map.keys()) == set(profile.suggested_tools)

    def test_unknown_tool_defaults_to_exploring(self) -> None:
        from ouroboros.orchestrator.profile_loader import EvidenceSchema

        custom = load_profile("code").model_copy(
            update={
                "suggested_tools": ("Read", "MysteryTool"),
                "evidence_schema": EvidenceSchema(),
            }
        )
        activity_map = ProfileBackedStrategy(custom).get_activity_map()
        assert activity_map["MysteryTool"] == ActivityType.EXPLORING
