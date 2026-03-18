"""Unit tests for inter-level context passing module."""

from __future__ import annotations

import pytest

from ouroboros.orchestrator.adapter import AgentMessage
from ouroboros.orchestrator.coordinator import CoordinatorReview, FileConflict
from ouroboros.orchestrator.level_context import (
    ACContextSummary,
    LevelContext,
    build_context_prompt,
    deserialize_level_contexts,
    extract_level_context,
    serialize_level_contexts,
)


class TestACContextSummary:
    """Tests for ACContextSummary dataclass."""

    def test_create_summary(self) -> None:
        """Test creating a basic summary."""
        summary = ACContextSummary(
            ac_index=0,
            ac_content="Create user model",
            success=True,
        )
        assert summary.ac_index == 0
        assert summary.ac_content == "Create user model"
        assert summary.success is True
        assert summary.tools_used == ()
        assert summary.files_modified == ()
        assert summary.key_output == ""

    def test_create_summary_with_details(self) -> None:
        """Test creating a summary with full details."""
        summary = ACContextSummary(
            ac_index=1,
            ac_content="Write API endpoints",
            success=True,
            tools_used=("Edit", "Read", "Write"),
            files_modified=("src/api.py", "src/models.py"),
            key_output="All endpoints implemented successfully",
        )
        assert summary.tools_used == ("Edit", "Read", "Write")
        assert len(summary.files_modified) == 2
        assert "endpoints" in summary.key_output

    def test_summary_is_frozen(self) -> None:
        """Test that ACContextSummary is immutable."""
        summary = ACContextSummary(ac_index=0, ac_content="Test", success=True)
        with pytest.raises(AttributeError):
            summary.success = False  # type: ignore


class TestLevelContext:
    """Tests for LevelContext dataclass."""

    def test_create_empty_context(self) -> None:
        """Test creating context with no ACs."""
        ctx = LevelContext(level_number=0)
        assert ctx.level_number == 0
        assert ctx.completed_acs == ()

    def test_to_prompt_text_with_successful_acs(self) -> None:
        """Test prompt text generation with successful ACs."""
        ctx = LevelContext(
            level_number=0,
            completed_acs=(
                ACContextSummary(
                    ac_index=0,
                    ac_content="Create user model with fields",
                    success=True,
                    files_modified=("src/models.py",),
                    key_output="User model created",
                ),
            ),
        )
        text = ctx.to_prompt_text()
        assert "AC 1" in text
        assert "src/models.py" in text
        assert "User model created" in text

    def test_to_prompt_text_empty_when_no_success(self) -> None:
        """Test prompt text is empty when no ACs succeeded."""
        ctx = LevelContext(
            level_number=0,
            completed_acs=(
                ACContextSummary(
                    ac_index=0,
                    ac_content="Failed AC",
                    success=False,
                ),
            ),
        )
        assert ctx.to_prompt_text() == ""

    def test_to_prompt_text_skips_failed_acs(self) -> None:
        """Test that failed ACs are excluded from prompt text."""
        ctx = LevelContext(
            level_number=0,
            completed_acs=(
                ACContextSummary(ac_index=0, ac_content="Success AC", success=True),
                ACContextSummary(ac_index=1, ac_content="Failed AC", success=False),
            ),
        )
        text = ctx.to_prompt_text()
        assert "AC 1" in text
        assert "Failed AC" not in text

    def test_to_prompt_text_truncates_many_files(self) -> None:
        """Test that file list is truncated when more than 5 files."""
        ctx = LevelContext(
            level_number=0,
            completed_acs=(
                ACContextSummary(
                    ac_index=0,
                    ac_content="Refactor all modules",
                    success=True,
                    files_modified=tuple(f"src/mod_{i}.py" for i in range(8)),
                ),
            ),
        )
        text = ctx.to_prompt_text()
        assert "+3 more" in text

    def test_context_is_frozen(self) -> None:
        """Test that LevelContext is immutable."""
        ctx = LevelContext(level_number=0)
        with pytest.raises(AttributeError):
            ctx.level_number = 1  # type: ignore


class TestBuildContextPrompt:
    """Tests for build_context_prompt function."""

    def test_empty_contexts(self) -> None:
        """Test returns empty string for no contexts."""
        assert build_context_prompt([]) == ""

    def test_single_level_context(self) -> None:
        """Test prompt from a single level context."""
        contexts = [
            LevelContext(
                level_number=0,
                completed_acs=(
                    ACContextSummary(
                        ac_index=0,
                        ac_content="Setup project",
                        success=True,
                        key_output="Project initialized",
                    ),
                ),
            ),
        ]
        prompt = build_context_prompt(contexts)
        assert "Previous Work Context" in prompt
        assert "Project initialized" in prompt

    def test_multiple_level_contexts(self) -> None:
        """Test prompt from multiple level contexts."""
        contexts = [
            LevelContext(
                level_number=0,
                completed_acs=(
                    ACContextSummary(ac_index=0, ac_content="Level 0 work", success=True),
                ),
            ),
            LevelContext(
                level_number=1,
                completed_acs=(
                    ACContextSummary(ac_index=1, ac_content="Level 1 work", success=True),
                ),
            ),
        ]
        prompt = build_context_prompt(contexts)
        assert "Level 0 work" in prompt
        assert "Level 1 work" in prompt

    def test_skips_levels_with_no_successes(self) -> None:
        """Test that levels with only failures produce empty prompt."""
        contexts = [
            LevelContext(
                level_number=0,
                completed_acs=(ACContextSummary(ac_index=0, ac_content="Failed", success=False),),
            ),
        ]
        assert build_context_prompt(contexts) == ""


class TestExtractLevelContext:
    """Tests for extract_level_context function."""

    def test_extract_from_empty_results(self) -> None:
        """Test extraction from empty result list."""
        ctx = extract_level_context([], level_num=0)
        assert ctx.level_number == 0
        assert ctx.completed_acs == ()

    def test_extract_basic_context(self) -> None:
        """Test extracting context from simple AC results."""
        results = [
            (0, "Create the model", True, (), "Model created successfully"),
        ]
        ctx = extract_level_context(results, level_num=1)
        assert ctx.level_number == 1
        assert len(ctx.completed_acs) == 1
        assert ctx.completed_acs[0].ac_index == 0
        assert ctx.completed_acs[0].success is True
        assert "Model created" in ctx.completed_acs[0].key_output

    def test_extract_tools_and_files(self) -> None:
        """Test that tools and modified files are extracted from messages."""
        messages = (
            AgentMessage(type="tool", content="", tool_name="Read"),
            AgentMessage(
                type="tool",
                content="",
                tool_name="Write",
                data={"tool_input": {"file_path": "src/main.py"}},
            ),
            AgentMessage(
                type="tool",
                content="",
                tool_name="Edit",
                data={"tool_input": {"file_path": "src/utils.py"}},
            ),
            AgentMessage(type="tool", content="", tool_name="Bash"),
        )
        results = [
            (0, "Implement feature", True, messages, "Feature implemented"),
        ]
        ctx = extract_level_context(results, level_num=0)
        summary = ctx.completed_acs[0]

        assert "Bash" in summary.tools_used
        assert "Edit" in summary.tools_used
        assert "Read" in summary.tools_used
        assert "Write" in summary.tools_used
        assert "src/main.py" in summary.files_modified
        assert "src/utils.py" in summary.files_modified

    def test_extract_truncates_key_output(self) -> None:
        """Test that key_output is truncated to max chars."""
        long_output = "x" * 500
        results = [
            (0, "Big task", True, (), long_output),
        ]
        ctx = extract_level_context(results, level_num=0)
        assert len(ctx.completed_acs[0].key_output) <= 200

    def test_extract_multiple_acs(self) -> None:
        """Test extracting context from multiple ACs."""
        results = [
            (0, "AC zero", True, (), "Done zero"),
            (1, "AC one", False, (), "Failed one"),
            (2, "AC two", True, (), "Done two"),
        ]
        ctx = extract_level_context(results, level_num=0)
        assert len(ctx.completed_acs) == 3
        assert ctx.completed_acs[0].success is True
        assert ctx.completed_acs[1].success is False
        assert ctx.completed_acs[2].success is True


class TestLevelContextSerialization:
    """Tests for serialize/deserialize round-trip of level contexts."""

    def test_round_trip_basic(self) -> None:
        """Test serialization round-trip for a basic context."""
        original = [
            LevelContext(
                level_number=0,
                completed_acs=(
                    ACContextSummary(
                        ac_index=0,
                        ac_content="Create model",
                        success=True,
                        tools_used=("Read", "Write"),
                        files_modified=("src/model.py",),
                        key_output="Model created",
                    ),
                ),
            ),
        ]
        restored = deserialize_level_contexts(serialize_level_contexts(original))
        assert len(restored) == 1
        assert restored[0].level_number == 0
        ac = restored[0].completed_acs[0]
        assert ac.ac_index == 0
        assert ac.ac_content == "Create model"
        assert ac.success is True
        assert ac.tools_used == ("Read", "Write")
        assert ac.files_modified == ("src/model.py",)
        assert ac.key_output == "Model created"

    def test_round_trip_with_coordinator_review(self) -> None:
        """Test serialization preserves coordinator review including conflicts."""
        review = CoordinatorReview(
            level_number=1,
            conflicts_detected=(
                FileConflict(
                    file_path="src/shared.py",
                    ac_indices=(0, 2),
                    resolved=True,
                    resolution_description="Merged imports",
                ),
            ),
            review_summary="Conflict resolved",
            fixes_applied=("merged imports",),
            warnings_for_next_level=("watch out for circular deps",),
            duration_seconds=2.5,
            session_id="sess_review",
        )
        original = [
            LevelContext(
                level_number=1,
                completed_acs=(ACContextSummary(ac_index=0, ac_content="AC", success=True),),
                coordinator_review=review,
            ),
        ]
        restored = deserialize_level_contexts(serialize_level_contexts(original))
        r = restored[0].coordinator_review
        assert r is not None
        assert r.level_number == 1
        assert r.review_summary == "Conflict resolved"
        assert r.fixes_applied == ("merged imports",)
        assert r.warnings_for_next_level == ("watch out for circular deps",)
        assert r.duration_seconds == 2.5
        assert r.session_id == "sess_review"
        assert len(r.conflicts_detected) == 1
        fc = r.conflicts_detected[0]
        assert fc.file_path == "src/shared.py"
        assert fc.ac_indices == (0, 2)
        assert fc.resolved is True
        assert fc.resolution_description == "Merged imports"

    def test_round_trip_empty(self) -> None:
        """Test serialization of empty context list."""
        assert deserialize_level_contexts(serialize_level_contexts([])) == []

    def test_round_trip_multiple_levels(self) -> None:
        """Test serialization of multiple levels."""
        original = [
            LevelContext(
                level_number=i,
                completed_acs=(ACContextSummary(ac_index=i, ac_content=f"AC {i}", success=True),),
            )
            for i in range(3)
        ]
        restored = deserialize_level_contexts(serialize_level_contexts(original))
        assert len(restored) == 3
        for i, ctx in enumerate(restored):
            assert ctx.level_number == i
            assert ctx.completed_acs[0].ac_index == i
