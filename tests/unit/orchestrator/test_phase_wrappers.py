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

    def test_ac_passes_through_verbatim(self, code_profile: ExecutionProfile) -> None:
        # Bot finding on #886 r3: ACs are free-form text and may
        # carry intentional leading indentation (indented code blocks,
        # nested bullets, YAML snippets). Stripping corrupts those
        # before they reach the leaf executor.
        ac = "   indented code:\n     pass\n"
        block = build_pre_block(code_profile, ac)
        # Every line is indented two more spaces; original indentation
        # survives intact inside the AC body.
        assert "     indented code:" in block  # 2 + 3 = 5 spaces
        assert "       pass" in block  # 2 + 5 = 7 spaces

    def test_trailing_whitespace_preserved(self, code_profile: ExecutionProfile) -> None:
        # Trailing whitespace can be load-bearing (e.g. markdown line
        # breaks via two trailing spaces). The wrapper must not eat it.
        ac = "ac body  "  # two trailing spaces
        block = build_pre_block(code_profile, ac)
        assert "  ac body  \n" in block

    def test_terminal_newline_preserved(self, code_profile: ExecutionProfile) -> None:
        # Bot finding on #886 r4: splitlines() dropped terminal newlines
        # and trailing blank lines, violating the verbatim contract for
        # ACs whose grammar requires a closing newline.
        ac = "line one\nline two\n"
        block = build_pre_block(code_profile, ac)
        # AC has two content lines + a trailing empty produced by the
        # closing \n. Indented output must keep the empty so the
        # section ends cleanly before the wrapper's joiner.
        assert "  line one\n  line two\n\n\nBefore" in block

    def test_trailing_blank_line_preserved(self, code_profile: ExecutionProfile) -> None:
        # ACs that intentionally end with a blank line (e.g. to terminate
        # a fenced code block) must round-trip verbatim.
        ac = "content\n\n"
        block = build_pre_block(code_profile, ac)
        # "content\n\n".split("\n") = ["content", "", ""]; indent skips
        # empty lines, so rejoined: "  content\n\n". The wrapper then
        # adds its own "\n\n" before "Before".
        assert "  content\n\n\n\nBefore" in block

    def test_multiline_ac_every_line_indented(self, code_profile: ExecutionProfile) -> None:
        # Bot finding on #886 r2: subsequent lines of a multiline AC
        # used to run flush-left and visually escape the AC section,
        # merging with the harness instructions below. Every non-empty
        # line must carry the two-space indent.
        multiline = "first line\nsecond line\nthird line"
        block = build_pre_block(code_profile, multiline)
        assert "  first line" in block
        assert "  second line" in block
        assert "  third line" in block
        # Flush-left lines (which would escape the section) must not
        # appear adjacent to the AC body.
        assert "\nsecond line" not in block
        assert "\nthird line" not in block

    def test_blank_lines_in_ac_preserved(self, code_profile: ExecutionProfile) -> None:
        # Blank lines stay blank (not "  ") so the AC's paragraph
        # structure survives untouched.
        ac = "paragraph one\n\nparagraph two"
        block = build_pre_block(code_profile, ac)
        assert "  paragraph one\n\n  paragraph two" in block


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

    def test_body_passes_through_verbatim(self, code_profile: ExecutionProfile) -> None:
        # Bot finding on #886 r2: body must NOT be normalized. Indented
        # code blocks, JSON examples, deliberately blank-prefixed
        # markdown must survive the wrapper unchanged.
        body = "\n\n  indented body\n  with blank-prefixed lines\n\n"
        wrapped = wrap_prompt(code_profile, "AC", body)
        assert wrapped.body == body

    def test_profile_distinction_reflected(self) -> None:
        c = wrap_prompt(load_profile("code"), "AC", "body").render()
        r = wrap_prompt(load_profile("research"), "AC", "body").render()
        a = wrap_prompt(load_profile("analysis"), "AC", "body").render()
        assert "testable_unit" in c
        assert "subtopic" in r
        assert "perspective" in a
        assert c != r != a
