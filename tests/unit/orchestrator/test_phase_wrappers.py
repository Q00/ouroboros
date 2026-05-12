"""Tests for ouroboros.orchestrator.phase_wrappers (RFC v2 #830, PR 5)."""

from __future__ import annotations

import pytest

from ouroboros.orchestrator.phase_wrappers import (
    WrappedPrompt,
    build_post_block,
    build_pre_block,
    wrap_prompt,
)
from ouroboros.orchestrator.profile_loader import (
    EvidenceSchema,
    ExecutionProfile,
    load_profile,
)


@pytest.fixture
def code_profile() -> ExecutionProfile:
    return load_profile("code")


class TestPreBlock:
    def test_includes_profile_and_axis(self, code_profile: ExecutionProfile) -> None:
        block = build_pre_block(code_profile, "Add caching layer")
        assert "'code'" in block
        assert "axis: testable_unit" in block
        assert "Add caching layer" in block

    def test_demands_restatement(self, code_profile: ExecutionProfile) -> None:
        block = build_pre_block(code_profile, "x")
        assert "restate" in block.lower()
        assert "precondition" in block.lower()

    def test_blocker_path_named(self, code_profile: ExecutionProfile) -> None:
        block = build_pre_block(code_profile, "x")
        assert "blocker" in block.lower()

    def test_strips_ac_whitespace(self, code_profile: ExecutionProfile) -> None:
        block = build_pre_block(code_profile, "   spaced AC\n\n")
        # Leading triple-space and trailing double-newline came from the
        # caller; the wrapper must strip them so the AC text sits flush
        # against the bullet indent.
        assert "   spaced AC" not in block
        assert "  spaced AC\n\nBefore" in block


class TestPostBlock:
    def test_lists_required_fields(self, code_profile: ExecutionProfile) -> None:
        block = build_post_block(code_profile)
        for required in code_profile.evidence_schema.required:
            assert required in block

    def test_lists_rejection_rules(self, code_profile: ExecutionProfile) -> None:
        block = build_post_block(code_profile)
        assert "tests_passed == []" in block

    def test_forbids_self_declared_done(self, code_profile: ExecutionProfile) -> None:
        block = build_post_block(code_profile)
        assert "Do not declare" in block
        assert "DONE" in block

    def test_demands_fenced_json(self, code_profile: ExecutionProfile) -> None:
        block = build_post_block(code_profile)
        assert "fenced JSON" in block

    def test_handles_empty_schema_gracefully(self) -> None:
        bare = ExecutionProfile(
            profile="bare",
            axis="a",
            min_unit="m",
            verifier_focus="v",
            evidence_schema=EvidenceSchema(),
        )
        block = build_post_block(bare)
        assert "no required evidence fields" in block
        assert "no automatic rejection rules" in block


class TestWrapPrompt:
    def test_returns_wrapped_prompt_with_three_parts(self, code_profile: ExecutionProfile) -> None:
        wrapped = wrap_prompt(code_profile, "AC text", "Body content here")
        assert isinstance(wrapped, WrappedPrompt)
        assert wrapped.body == "Body content here"
        assert "[PRE" in wrapped.pre
        assert "[POST" in wrapped.post

    def test_render_joins_with_blank_lines(self, code_profile: ExecutionProfile) -> None:
        wrapped = wrap_prompt(code_profile, "AC", "Body")
        rendered = wrapped.render()
        assert rendered.startswith("[PRE")
        assert rendered.endswith(wrapped.post)
        # PRE / body / POST are double-newline separated.
        assert rendered.count("\n\n") >= 2

    def test_body_is_trimmed(self, code_profile: ExecutionProfile) -> None:
        wrapped = wrap_prompt(code_profile, "AC", "\n\n  body\n\n")
        assert wrapped.body == "body"

    def test_profile_distinction_reflected(self) -> None:
        c = wrap_prompt(load_profile("code"), "AC", "body").render()
        r = wrap_prompt(load_profile("research"), "AC", "body").render()
        a = wrap_prompt(load_profile("analysis"), "AC", "body").render()
        assert "testable_unit" in c
        assert "subtopic" in r
        assert "perspective" in a
        assert c != r != a
