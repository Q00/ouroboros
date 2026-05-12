"""Tests for ouroboros.orchestrator.decomposition_params (RFC v2 #830, PR 4)."""

from __future__ import annotations

import pytest

from ouroboros.orchestrator.decomposition_params import (
    DEFAULT_MAX_BRANCHING,
    DEFAULT_MIN_BRANCHING,
    DecompositionParams,
    build_decomposition_system_prompt,
    build_decomposition_user_prompt,
    params_from_profile,
)
from ouroboros.orchestrator.profile_loader import load_profile


class TestParamsFromProfile:
    def test_projects_code_profile(self) -> None:
        params = params_from_profile(load_profile("code"))
        assert params.profile_name == "code"
        assert params.axis == "testable_unit"
        assert "function" in params.min_unit
        assert params.cut_signal  # non-empty

    def test_defaults_branching(self) -> None:
        params = params_from_profile(load_profile("research"))
        assert params.min_branching == DEFAULT_MIN_BRANCHING == 2
        assert params.max_branching == DEFAULT_MAX_BRANCHING == 5

    def test_override_branching(self) -> None:
        params = params_from_profile(load_profile("analysis"), min_branching=3, max_branching=4)
        assert params.min_branching == 3
        assert params.max_branching == 4


class TestInvariants:
    def test_min_branching_must_be_positive(self) -> None:
        with pytest.raises(ValueError, match="min_branching must be >= 1"):
            DecompositionParams(
                profile_name="x",
                axis="a",
                min_unit="m",
                cut_signal="",
                min_branching=0,
                max_branching=5,
            )

    def test_max_below_min_rejected(self) -> None:
        with pytest.raises(ValueError, match="max_branching"):
            DecompositionParams(
                profile_name="x",
                axis="a",
                min_unit="m",
                cut_signal="",
                min_branching=3,
                max_branching=2,
            )


class TestSystemPrompt:
    def test_mentions_axis_and_min_unit(self) -> None:
        params = params_from_profile(load_profile("code"))
        prompt = build_decomposition_system_prompt(params)
        assert "'code'" in prompt
        assert "testable_unit" in prompt
        assert params.min_unit in prompt

    def test_distinguishes_profiles(self) -> None:
        a = build_decomposition_system_prompt(params_from_profile(load_profile("code")))
        b = build_decomposition_system_prompt(params_from_profile(load_profile("research")))
        c = build_decomposition_system_prompt(params_from_profile(load_profile("analysis")))
        assert a != b != c
        assert "testable_unit" in a
        assert "subtopic" in b
        assert "perspective" in c


class TestUserPrompt:
    def test_carries_ac_and_goal(self) -> None:
        params = params_from_profile(load_profile("code"))
        prompt = build_decomposition_user_prompt(
            params, ac_label="AC #2.1", ac_content="Add caching layer", seed_goal="Speed up API"
        )
        assert "AC #2.1" in prompt
        assert "Add caching layer" in prompt
        assert "Speed up API" in prompt
        assert "testable_unit" in prompt
        assert params.cut_signal in prompt

    def test_branching_bounds_in_prompt(self) -> None:
        params = params_from_profile(load_profile("code"), min_branching=2, max_branching=4)
        prompt = build_decomposition_user_prompt(
            params, ac_label="x", ac_content="y", seed_goal="z"
        )
        assert "2-4 sub-ACs" in prompt

    def test_atomic_instruction_present(self) -> None:
        params = params_from_profile(load_profile("code"))
        prompt = build_decomposition_user_prompt(
            params, ac_label="x", ac_content="y", seed_goal="z"
        )
        assert "ATOMIC" in prompt
        assert "JSON array" in prompt

    def test_omits_cut_signal_line_when_empty(self) -> None:
        params = DecompositionParams(
            profile_name="bare",
            axis="a",
            min_unit="m",
            cut_signal="",
        )
        prompt = build_decomposition_user_prompt(
            params, ac_label="x", ac_content="y", seed_goal="z"
        )
        assert "A sub-AC is small enough when:" not in prompt
