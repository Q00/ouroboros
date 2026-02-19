"""Tests for the Level Coordinator module.

Tests cover:
- FileConflict and CoordinatorReview data models
- detect_file_conflicts() with various scenarios
- _collect_file_modifications() for atomic and decomposed results
- _build_review_prompt() formatting
- _parse_review_response() JSON parsing and fallback
- build_context_prompt() integration with coordinator_review
"""

from __future__ import annotations

import pytest

from ouroboros.orchestrator.adapter import AgentMessage
from ouroboros.orchestrator.coordinator import (
    CoordinatorReview,
    FileConflict,
    LevelCoordinator,
    _build_review_prompt,
    _collect_file_modifications,
    _parse_review_response,
)
from ouroboros.orchestrator.level_context import (
    ACContextSummary,
    LevelContext,
    build_context_prompt,
)
from ouroboros.orchestrator.parallel_executor import ACExecutionResult

# =============================================================================
# Data Model Tests
# =============================================================================


class TestFileConflict:
    """Tests for FileConflict dataclass."""

    def test_basic_creation(self):
        conflict = FileConflict(
            file_path="src/app.py",
            ac_indices=(0, 2),
        )
        assert conflict.file_path == "src/app.py"
        assert conflict.ac_indices == (0, 2)
        assert conflict.resolved is False
        assert conflict.resolution_description == ""

    def test_resolved_conflict(self):
        conflict = FileConflict(
            file_path="src/app.py",
            ac_indices=(0, 1),
            resolved=True,
            resolution_description="Merged imports from both ACs",
        )
        assert conflict.resolved is True
        assert conflict.resolution_description == "Merged imports from both ACs"

    def test_frozen(self):
        conflict = FileConflict(file_path="a.py", ac_indices=(0,))
        with pytest.raises(AttributeError):
            conflict.file_path = "b.py"


class TestCoordinatorReview:
    """Tests for CoordinatorReview dataclass."""

    def test_basic_creation(self):
        review = CoordinatorReview(level_number=1)
        assert review.level_number == 1
        assert review.conflicts_detected == ()
        assert review.review_summary == ""
        assert review.fixes_applied == ()
        assert review.warnings_for_next_level == ()
        assert review.duration_seconds == 0.0
        assert review.session_id is None

    def test_full_review(self):
        conflict = FileConflict(
            file_path="src/routes.py",
            ac_indices=(0, 1),
            resolved=True,
        )
        review = CoordinatorReview(
            level_number=2,
            conflicts_detected=(conflict,),
            review_summary="Resolved import conflict in routes.py",
            fixes_applied=("Merged duplicate import statements",),
            warnings_for_next_level=("Ensure routes are registered in main.py",),
            duration_seconds=5.3,
            session_id="sess_abc",
        )
        assert len(review.conflicts_detected) == 1
        assert review.conflicts_detected[0].resolved is True
        assert len(review.fixes_applied) == 1
        assert len(review.warnings_for_next_level) == 1

    def test_frozen(self):
        review = CoordinatorReview(level_number=1)
        with pytest.raises(AttributeError):
            review.level_number = 2


# =============================================================================
# detect_file_conflicts Tests
# =============================================================================


def _make_result(
    ac_index: int,
    tool_calls: list[tuple[str, str]] | None = None,
    sub_results: list[ACExecutionResult] | None = None,
) -> ACExecutionResult:
    """Helper to create ACExecutionResult with specific tool calls.

    Args:
        ac_index: AC index.
        tool_calls: List of (tool_name, file_path) tuples.
        sub_results: Optional sub-results for decomposed ACs.
    """
    messages = []
    for tool_name, file_path in tool_calls or []:
        messages.append(
            AgentMessage(
                type="assistant",
                content=f"Using {tool_name}",
                tool_name=tool_name,
                data={"tool_input": {"file_path": file_path}},
            )
        )
    return ACExecutionResult(
        ac_index=ac_index,
        ac_content=f"AC {ac_index + 1} content",
        success=True,
        messages=tuple(messages),
        sub_results=tuple(sub_results or []),
    )


class TestDetectFileConflicts:
    """Tests for LevelCoordinator.detect_file_conflicts()."""

    def test_no_results(self):
        conflicts = LevelCoordinator.detect_file_conflicts([])
        assert conflicts == []

    def test_no_conflicts_different_files(self):
        results = [
            _make_result(0, [("Write", "src/a.py")]),
            _make_result(1, [("Edit", "src/b.py")]),
        ]
        conflicts = LevelCoordinator.detect_file_conflicts(results)
        assert conflicts == []

    def test_single_conflict(self):
        results = [
            _make_result(0, [("Write", "src/app.py")]),
            _make_result(1, [("Edit", "src/app.py")]),
        ]
        conflicts = LevelCoordinator.detect_file_conflicts(results)
        assert len(conflicts) == 1
        assert conflicts[0].file_path == "src/app.py"
        assert conflicts[0].ac_indices == (0, 1)
        assert conflicts[0].resolved is False

    def test_multiple_conflicts(self):
        results = [
            _make_result(0, [("Write", "src/a.py"), ("Edit", "src/b.py")]),
            _make_result(1, [("Edit", "src/a.py")]),
            _make_result(2, [("Write", "src/b.py"), ("Write", "src/c.py")]),
        ]
        conflicts = LevelCoordinator.detect_file_conflicts(results)
        assert len(conflicts) == 2
        # Sorted by file path
        assert conflicts[0].file_path == "src/a.py"
        assert conflicts[0].ac_indices == (0, 1)
        assert conflicts[1].file_path == "src/b.py"
        assert conflicts[1].ac_indices == (0, 2)

    def test_three_way_conflict(self):
        results = [
            _make_result(0, [("Write", "src/shared.py")]),
            _make_result(1, [("Edit", "src/shared.py")]),
            _make_result(2, [("Edit", "src/shared.py")]),
        ]
        conflicts = LevelCoordinator.detect_file_conflicts(results)
        assert len(conflicts) == 1
        assert conflicts[0].ac_indices == (0, 1, 2)

    def test_ignores_read_and_other_tools(self):
        results = [
            _make_result(
                0,
                [("Write", "src/app.py")],
            ),
            ACExecutionResult(
                ac_index=1,
                ac_content="AC 2",
                success=True,
                messages=(
                    AgentMessage(
                        type="assistant",
                        content="Reading",
                        tool_name="Read",
                        data={"tool_input": {"file_path": "src/app.py"}},
                    ),
                    AgentMessage(
                        type="assistant",
                        content="Grepping",
                        tool_name="Grep",
                        data={"tool_input": {"pattern": "import"}},
                    ),
                ),
            ),
        ]
        conflicts = LevelCoordinator.detect_file_conflicts(results)
        assert conflicts == []

    def test_same_ac_multiple_edits_no_conflict(self):
        """Same AC editing same file multiple times is NOT a conflict."""
        results = [
            _make_result(0, [("Write", "src/app.py"), ("Edit", "src/app.py")]),
        ]
        conflicts = LevelCoordinator.detect_file_conflicts(results)
        assert conflicts == []

    def test_decomposed_sub_acs_inherit_parent_index(self):
        """Sub-AC modifications are attributed to the parent AC index."""
        sub_result = ACExecutionResult(
            ac_index=100,  # Sub-AC index (parent * 100 + sub)
            ac_content="Sub-AC 1",
            success=True,
            messages=(
                AgentMessage(
                    type="assistant",
                    content="Writing",
                    tool_name="Write",
                    data={"tool_input": {"file_path": "src/shared.py"}},
                ),
            ),
        )
        results = [
            _make_result(0, sub_results=[sub_result]),
            _make_result(1, [("Edit", "src/shared.py")]),
        ]
        conflicts = LevelCoordinator.detect_file_conflicts(results)
        assert len(conflicts) == 1
        assert conflicts[0].ac_indices == (0, 1)

    def test_no_file_path_in_tool_input(self):
        """Messages without file_path in tool_input are safely ignored."""
        results = [
            ACExecutionResult(
                ac_index=0,
                ac_content="AC 1",
                success=True,
                messages=(
                    AgentMessage(
                        type="assistant",
                        content="Writing",
                        tool_name="Write",
                        data={"tool_input": {}},  # No file_path
                    ),
                ),
            ),
        ]
        conflicts = LevelCoordinator.detect_file_conflicts(results)
        assert conflicts == []


# =============================================================================
# _collect_file_modifications Tests
# =============================================================================


class TestCollectFileModifications:
    """Tests for _collect_file_modifications helper."""

    def test_empty_messages(self):
        result = _make_result(0)
        acc: dict[str, set[int]] = {}
        _collect_file_modifications(result, acc)
        assert acc == {}

    def test_write_and_edit(self):
        result = _make_result(0, [("Write", "a.py"), ("Edit", "b.py")])
        acc: dict[str, set[int]] = {}
        _collect_file_modifications(result, acc)
        assert acc == {"a.py": {0}, "b.py": {0}}

    def test_nested_sub_results(self):
        sub = ACExecutionResult(
            ac_index=100,
            ac_content="sub",
            success=True,
            messages=(
                AgentMessage(
                    type="assistant",
                    content="w",
                    tool_name="Edit",
                    data={"tool_input": {"file_path": "deep.py"}},
                ),
            ),
        )
        parent = _make_result(0, [("Write", "top.py")], sub_results=[sub])
        acc: dict[str, set[int]] = {}
        _collect_file_modifications(parent, acc)
        assert acc == {"top.py": {0}, "deep.py": {0}}


# =============================================================================
# _build_review_prompt Tests
# =============================================================================


class TestBuildReviewPrompt:
    """Tests for _build_review_prompt."""

    def test_basic_prompt(self):
        conflicts = [
            FileConflict(file_path="src/app.py", ac_indices=(0, 1)),
        ]
        level_ctx = LevelContext(
            level_number=1,
            completed_acs=(
                ACContextSummary(ac_index=0, ac_content="Create model", success=True),
                ACContextSummary(ac_index=1, ac_content="Create routes", success=True),
            ),
        )
        prompt = _build_review_prompt(conflicts, level_ctx, 1)

        assert "Level 1" in prompt
        assert "src/app.py" in prompt
        assert "AC 1" in prompt
        assert "AC 2" in prompt
        assert "Read tool" in prompt
        assert "git diff" in prompt

    def test_multiple_conflicts(self):
        conflicts = [
            FileConflict(file_path="a.py", ac_indices=(0, 2)),
            FileConflict(file_path="b.py", ac_indices=(1, 2)),
        ]
        level_ctx = LevelContext(level_number=2, completed_acs=())
        prompt = _build_review_prompt(conflicts, level_ctx, 2)
        assert "a.py" in prompt
        assert "b.py" in prompt


# =============================================================================
# _parse_review_response Tests
# =============================================================================


class TestParseReviewResponse:
    """Tests for _parse_review_response."""

    def test_valid_json_response(self):
        response = """I've reviewed the conflicts.

```json
{
  "review_summary": "Merged duplicate imports",
  "fixes_applied": ["Combined import statements in app.py"],
  "warnings_for_next_level": ["Check route registration"],
  "conflicts_resolved": ["src/app.py"]
}
```
"""
        conflicts = [FileConflict(file_path="src/app.py", ac_indices=(0, 1))]
        review = _parse_review_response(response, conflicts, 1, 3.5, "sess_1")

        assert review.level_number == 1
        assert review.review_summary == "Merged duplicate imports"
        assert review.fixes_applied == ("Combined import statements in app.py",)
        assert review.warnings_for_next_level == ("Check route registration",)
        assert review.duration_seconds == 3.5
        assert review.session_id == "sess_1"
        assert review.conflicts_detected[0].resolved is True

    def test_bare_json_response(self):
        response = '{"review_summary": "All good", "fixes_applied": [], "warnings_for_next_level": [], "conflicts_resolved": []}'
        review = _parse_review_response(response, [], 2, 1.0, None)
        assert review.review_summary == "All good"

    def test_invalid_json_fallback(self):
        response = "I couldn't parse anything, but here's my review."
        review = _parse_review_response(response, [], 1, 2.0, None)
        assert review.review_summary == response

    def test_empty_response_fallback(self):
        review = _parse_review_response("", [], 1, 0.5, None)
        assert review.review_summary == "No review output"

    def test_unresolved_conflicts_stay_unresolved(self):
        response = '```json\n{"review_summary": "x", "fixes_applied": [], "warnings_for_next_level": [], "conflicts_resolved": ["a.py"]}\n```'
        conflicts = [
            FileConflict(file_path="a.py", ac_indices=(0, 1)),
            FileConflict(file_path="b.py", ac_indices=(0, 2)),
        ]
        review = _parse_review_response(response, conflicts, 1, 1.0, None)
        assert review.conflicts_detected[0].resolved is True
        assert review.conflicts_detected[1].resolved is False

    def test_partial_json_missing_fields(self):
        response = '```json\n{"review_summary": "partial"}\n```'
        review = _parse_review_response(response, [], 1, 1.0, None)
        assert review.review_summary == "partial"
        assert review.fixes_applied == ()
        assert review.warnings_for_next_level == ()


# =============================================================================
# build_context_prompt Integration with CoordinatorReview
# =============================================================================


class TestBuildContextPromptWithReview:
    """Tests that build_context_prompt() includes coordinator review."""

    def test_no_review(self):
        contexts = [
            LevelContext(
                level_number=1,
                completed_acs=(ACContextSummary(ac_index=0, ac_content="AC 1", success=True),),
            ),
        ]
        prompt = build_context_prompt(contexts)
        assert "Previous Work Context" in prompt
        assert "Coordinator Review" not in prompt

    def test_with_review(self):
        review = CoordinatorReview(
            level_number=1,
            review_summary="Fixed merge conflict in app.py",
            fixes_applied=("Merged imports",),
            warnings_for_next_level=("Register new routes in main.py",),
        )
        contexts = [
            LevelContext(
                level_number=1,
                completed_acs=(ACContextSummary(ac_index=0, ac_content="AC 1", success=True),),
                coordinator_review=review,
            ),
        ]
        prompt = build_context_prompt(contexts)
        assert "Coordinator Review (Level 1)" in prompt
        assert "Fixed merge conflict in app.py" in prompt
        assert "Merged imports" in prompt
        assert "WARNING: Register new routes in main.py" in prompt

    def test_review_with_empty_warnings(self):
        review = CoordinatorReview(
            level_number=1,
            review_summary="No issues found",
        )
        contexts = [
            LevelContext(
                level_number=1,
                completed_acs=(ACContextSummary(ac_index=0, ac_content="AC 1", success=True),),
                coordinator_review=review,
            ),
        ]
        prompt = build_context_prompt(contexts)
        assert "No issues found" in prompt
        assert "WARNING" not in prompt

    def test_multiple_levels_with_mixed_reviews(self):
        """Only levels with reviews include review sections."""
        review = CoordinatorReview(
            level_number=2,
            review_summary="Conflict resolved",
            warnings_for_next_level=("Watch out for X",),
        )
        contexts = [
            LevelContext(
                level_number=1,
                completed_acs=(ACContextSummary(ac_index=0, ac_content="AC 1", success=True),),
                # No coordinator_review
            ),
            LevelContext(
                level_number=2,
                completed_acs=(ACContextSummary(ac_index=1, ac_content="AC 2", success=True),),
                coordinator_review=review,
            ),
        ]
        prompt = build_context_prompt(contexts)
        assert "Coordinator Review (Level 2)" in prompt
        assert "Coordinator Review (Level 1)" not in prompt
        assert "Watch out for X" in prompt
