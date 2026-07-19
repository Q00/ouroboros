"""Unit tests for ouroboros.bigbang.inner_guidance.

The module is the wrapper-agnostic contract for steering over
InterviewEngine: a declarative invariant registry plus the
budget-extension composition any steering wrapper can reuse.
"""

from pathlib import Path
from unittest.mock import MagicMock

from ouroboros.bigbang.inner_guidance import (
    INNER_GUIDANCE_INVARIANTS,
    compose_steered_prompt,
    reserve_steering_extension,
)
from ouroboros.bigbang.interview import InterviewEngine, InterviewState

_CUSTOM_STEERING = (
    "You are a QA-focused steering layer.\n"
    "Prefer questions that expose untested acceptance criteria.\n\n"
    "Core QA policy: every requirement must state how it will be verified."
)
_CUSTOM_MARKER = "Core QA policy"


def _make_inner(tmp_path: Path) -> InterviewEngine:
    return InterviewEngine(llm_adapter=MagicMock(), state_dir=tmp_path)


class TestInvariantRegistry:
    """The registry is the declared contract, readable without the code."""

    def test_registry_declares_named_explained_invariants(self) -> None:
        names = [invariant.name for invariant in INNER_GUIDANCE_INVARIANTS]
        assert names == [
            "initial-context",
            "answer-prefix-legend",
            "brownfield-intent-hint",
            "ambiguity-snapshot",
            "perspective-panel",
            "base-prompt-sections",
        ]
        assert all(invariant.why for invariant in INNER_GUIDANCE_INVARIANTS)

    def test_invariants_resolve_against_engine_and_state(self, tmp_path: Path) -> None:
        inner = _make_inner(tmp_path)
        state = InterviewState(
            interview_id="t_resolve",
            initial_context="Build a task manager",
            ambiguity_score=0.5,
            ambiguity_breakdown={
                "goal_clarity": {
                    "name": "Goal Clarity",
                    "clarity_score": 0.5,
                    "justification": "partly clear",
                }
            },
        )
        for invariant in INNER_GUIDANCE_INVARIANTS:
            texts = invariant.resolve(inner, state)
            assert isinstance(texts, tuple)


class TestGenericWrapperReuse:
    """A hypothetical second wrapper gets the same guarantees for free."""

    def test_custom_steering_rides_in_reserved_extension(self, tmp_path: Path) -> None:
        inner = _make_inner(tmp_path)
        reserve_steering_extension(inner, _CUSTOM_STEERING)
        extension = len(_CUSTOM_STEERING) + 2
        assert (
            InterviewEngine._MAX_SYSTEM_PROMPT_CHARS + extension == inner._MAX_SYSTEM_PROMPT_CHARS
        )

        state = InterviewState(interview_id="t_generic", initial_context="Build a task manager")
        prompt = compose_steered_prompt(
            inner=inner,
            build=inner._build_system_prompt,
            steering=_CUSTOM_STEERING,
            state=state,
            shed_last_marker=_CUSTOM_MARKER,
        )
        assert prompt.startswith(_CUSTOM_STEERING)
        assert len(prompt) <= inner._MAX_SYSTEM_PROMPT_CHARS
        # The inner portion is byte-identical to a designed-budget build.
        dev_build = inner._build_system_prompt(
            state, max_chars=InterviewEngine._MAX_SYSTEM_PROMPT_CHARS
        )
        assert prompt[len(_CUSTOM_STEERING) + 2 :] == dev_build

    def test_custom_marker_paragraph_outlives_others_under_tight_caps(self, tmp_path: Path) -> None:
        inner = _make_inner(tmp_path)
        reserve_steering_extension(inner, _CUSTOM_STEERING)
        state = InterviewState(interview_id="t_generic_tight", initial_context="Build an app")
        # Budget fits only the core-policy paragraph, not the supporting one.
        cap = InterviewEngine._MIN_SYSTEM_PROMPT_CHARS + 134
        prompt = compose_steered_prompt(
            inner=inner,
            build=inner._build_system_prompt,
            steering=_CUSTOM_STEERING,
            state=state,
            max_chars=cap,
            shed_last_marker=_CUSTOM_MARKER,
        )
        assert len(prompt) <= cap
        assert _CUSTOM_MARKER in prompt
        assert "QA-focused steering layer" not in prompt
