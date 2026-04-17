"""Unit tests for inter-level context passing module."""

from __future__ import annotations

import pytest

from ouroboros.orchestrator.adapter import AgentMessage
from ouroboros.orchestrator.coordinator import CoordinatorReview, FileConflict
from ouroboros.orchestrator.level_context import (
    _MAX_FILE_SIZE_BYTES,
    _MAX_FILES_FOR_API,
    ACContextSummary,
    LevelContext,
    _build_public_api_summary,
    _extract_public_api,
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

    def test_build_context_prompt_with_coordinator_review_no_successes(self) -> None:
        """Test coordinator review is preserved even when no ACs succeeded."""
        review = CoordinatorReview(
            level_number=0,
            conflicts_detected=(),
            review_summary="Merge conflict detected in shared.py",
            fixes_applied=("resolved import ordering",),
            warnings_for_next_level=("watch for circular imports",),
            duration_seconds=1.0,
            session_id="sess_review",
        )
        contexts = [
            LevelContext(
                level_number=0,
                completed_acs=(
                    ACContextSummary(ac_index=0, ac_content="Failed AC", success=False),
                ),
                coordinator_review=review,
            ),
        ]
        prompt = build_context_prompt(contexts)
        assert prompt != ""
        assert "Coordinator Review" in prompt
        assert "Merge conflict detected" in prompt
        assert "resolved import ordering" in prompt
        assert "watch for circular imports" in prompt
        # Should NOT contain "Previous Work Context" since no ACs succeeded
        assert "Previous Work Context" not in prompt


class TestExtractLevelContext:
    """Tests for extract_level_context function."""

    def test_extract_from_empty_results(self, tmp_path: object) -> None:
        """Test extraction from empty result list."""
        ctx = extract_level_context([], level_num=0, workspace_root=str(tmp_path))
        assert ctx.level_number == 0
        assert ctx.completed_acs == ()

    def test_extract_basic_context(self, tmp_path: object) -> None:
        """Test extracting context from simple AC results."""
        results = [
            (0, "Create the model", True, (), "Model created successfully"),
        ]
        ctx = extract_level_context(results, level_num=1, workspace_root=str(tmp_path))
        assert ctx.level_number == 1
        assert len(ctx.completed_acs) == 1
        assert ctx.completed_acs[0].ac_index == 0
        assert ctx.completed_acs[0].success is True
        assert "Model created" in ctx.completed_acs[0].key_output

    def test_extract_tools_and_files(self, tmp_path: object) -> None:
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
        ctx = extract_level_context(results, level_num=0, workspace_root=str(tmp_path))
        summary = ctx.completed_acs[0]

        assert "Bash" in summary.tools_used
        assert "Edit" in summary.tools_used
        assert "Read" in summary.tools_used
        assert "Write" in summary.tools_used
        assert "src/main.py" in summary.files_modified
        assert "src/utils.py" in summary.files_modified

    def test_extract_truncates_key_output(self, tmp_path: object) -> None:
        """Test that key_output is truncated to max chars."""
        long_output = "x" * 500
        results = [
            (0, "Big task", True, (), long_output),
        ]
        ctx = extract_level_context(results, level_num=0, workspace_root=str(tmp_path))
        assert len(ctx.completed_acs[0].key_output) <= 200

    def test_extract_multiple_acs(self, tmp_path: object) -> None:
        """Test extracting context from multiple ACs."""
        results = [
            (0, "AC zero", True, (), "Done zero"),
            (1, "AC one", False, (), "Failed one"),
            (2, "AC two", True, (), "Done two"),
        ]
        ctx = extract_level_context(results, level_num=0, workspace_root=str(tmp_path))
        assert len(ctx.completed_acs) == 3
        assert ctx.completed_acs[0].success is True
        assert ctx.completed_acs[1].success is False
        assert ctx.completed_acs[2].success is True

    def test_extract_notebook_edit_file_tracking(self, tmp_path: object) -> None:
        """Test that NotebookEdit file paths are tracked alongside Write/Edit."""
        messages = (
            AgentMessage(
                type="tool",
                content="",
                tool_name="NotebookEdit",
                data={"tool_input": {"file_path": "notebooks/analysis.ipynb"}},
            ),
            AgentMessage(
                type="tool",
                content="",
                tool_name="Write",
                data={"tool_input": {"file_path": "src/main.py"}},
            ),
        )
        results = [
            (0, "Update notebook and code", True, messages, "Done"),
        ]
        ctx = extract_level_context(results, level_num=0, workspace_root=str(tmp_path))
        summary = ctx.completed_acs[0]
        assert "notebooks/analysis.ipynb" in summary.files_modified
        assert "src/main.py" in summary.files_modified
        assert "NotebookEdit" in summary.tools_used


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

    def test_round_trip_preserves_public_api(self) -> None:
        """Test that public_api field survives serialization round-trip."""
        original = [
            LevelContext(
                level_number=0,
                completed_acs=(
                    ACContextSummary(
                        ac_index=0,
                        ac_content="Create service",
                        success=True,
                        files_modified=("src/service.py",),
                        key_output="Done",
                        public_api="service.py: class UserService, def get_user(id: str) -> User",
                    ),
                ),
            ),
        ]
        restored = deserialize_level_contexts(serialize_level_contexts(original))
        ac = restored[0].completed_acs[0]
        assert ac.public_api == "service.py: class UserService, def get_user(id: str) -> User"


class TestExtractPublicApi:
    """Tests for _extract_public_api signature extraction."""

    def test_python_class_and_function(self, tmp_path: object) -> None:
        """Test extracting Python class and function signatures."""
        import pathlib

        p = pathlib.Path(str(tmp_path)) / "service.py"
        p.write_text(
            "class UserService:\n"
            "    pass\n"
            "\n"
            "def get_user(id: str) -> User:\n"
            "    pass\n"
            "\n"
            "async def create_user(name: str, email: str) -> User:\n"
            "    pass\n"
        )
        sigs = _extract_public_api(str(p), str(tmp_path))
        assert "class UserService" in sigs
        assert "def get_user(id: str) -> User" in sigs
        assert any("async def create_user" in s for s in sigs)

    def test_python_skips_private(self, tmp_path: object) -> None:
        """Test that private (underscore-prefixed) names are skipped."""
        import pathlib

        p = pathlib.Path(str(tmp_path)) / "internal.py"
        p.write_text(
            "class _InternalHelper:\n"
            "    pass\n"
            "\n"
            "def _private_fn():\n"
            "    pass\n"
            "\n"
            "def public_fn() -> str:\n"
            "    pass\n"
        )
        sigs = _extract_public_api(str(p), str(tmp_path))
        assert len(sigs) == 1
        assert "public_fn" in sigs[0]

    def test_python_multiline_signature(self, tmp_path: object) -> None:
        """Test extracting multi-line function signatures."""
        import pathlib

        p = pathlib.Path(str(tmp_path)) / "multi.py"
        p.write_text(
            "def complex_function(\n"
            "    arg1: str,\n"
            "    arg2: int,\n"
            "    arg3: list[str],\n"
            ") -> dict[str, Any]:\n"
            "    pass\n"
        )
        sigs = _extract_public_api(str(p), str(tmp_path))
        assert len(sigs) == 1
        assert "arg1: str" in sigs[0]
        assert "arg2: int" in sigs[0]
        assert "-> dict[str, Any]" in sigs[0]

    def test_typescript_exports(self, tmp_path: object) -> None:
        """Test extracting TypeScript exported signatures."""
        import pathlib

        p = pathlib.Path(str(tmp_path)) / "service.ts"
        p.write_text(
            "export function getUser(id: string): User {\n"
            "  return db.get(id);\n"
            "}\n"
            "\n"
            "export class UserController {\n"
            "  constructor() {}\n"
            "}\n"
            "\n"
            "export interface UserDTO {\n"
            "  name: string;\n"
            "}\n"
            "\n"
            "function privateHelper() {}\n"
        )
        sigs = _extract_public_api(str(p), str(tmp_path))
        assert any("getUser" in s for s in sigs)
        assert any("UserController" in s for s in sigs)
        assert any("UserDTO" in s for s in sigs)
        assert not any("privateHelper" in s for s in sigs)

    def test_go_exports(self, tmp_path: object) -> None:
        """Test extracting Go exported (capitalized) signatures."""
        import pathlib

        p = pathlib.Path(str(tmp_path)) / "service.go"
        p.write_text(
            "func GetUser(id string) (*User, error) {\n"
            "    return nil, nil\n"
            "}\n"
            "\n"
            "type UserService struct {\n"
            "    db *DB\n"
            "}\n"
            "\n"
            "func privateHelper() {}\n"
        )
        sigs = _extract_public_api(str(p), str(tmp_path))
        assert any("GetUser" in s for s in sigs)
        assert any("UserService" in s for s in sigs)
        assert not any("privateHelper" in s for s in sigs)

    def test_nonexistent_file(self, tmp_path: object) -> None:
        """Test that nonexistent files return empty list."""
        # Point both file and workspace inside tmp_path so rejection is
        # driven by OSError from getsize(), not by containment.
        assert _extract_public_api(str(tmp_path) + "/nonexistent.py", str(tmp_path)) == []

    def test_unsupported_extension(self, tmp_path: object) -> None:
        """Test that unsupported file types return empty list."""
        import pathlib

        p = pathlib.Path(str(tmp_path)) / "data.json"
        p.write_text('{"key": "value"}')
        assert _extract_public_api(str(p), str(tmp_path)) == []

    def test_file_exceeding_size_limit_returns_empty(self, tmp_path: object) -> None:
        """Test that files larger than _MAX_FILE_SIZE_BYTES are skipped."""
        import pathlib

        p = pathlib.Path(str(tmp_path)) / "huge.py"
        # Write a file just over the limit
        p.write_text("class Big:\n    pass\n" + "x" * (_MAX_FILE_SIZE_BYTES + 1))
        assert _extract_public_api(str(p), str(tmp_path)) == []

    def test_file_within_size_limit_is_read(self, tmp_path: object) -> None:
        """Test that files within _MAX_FILE_SIZE_BYTES are read normally."""
        import pathlib

        p = pathlib.Path(str(tmp_path)) / "small.py"
        p.write_text("class Small:\n    pass\n")
        sigs = _extract_public_api(str(p), str(tmp_path))
        assert "class Small" in sigs

    def test_symlink_resolved_before_read(self, tmp_path: object) -> None:
        """Test that symlinks are resolved via realpath before reading."""
        import pathlib

        real_file = pathlib.Path(str(tmp_path)) / "real.py"
        real_file.write_text("class RealClass:\n    pass\n")
        link = pathlib.Path(str(tmp_path)) / "link.py"
        link.symlink_to(real_file)

        sigs = _extract_public_api(str(link), str(tmp_path))
        assert "class RealClass" in sigs

    def test_symlink_to_large_file_returns_empty(self, tmp_path: object) -> None:
        """Test that symlinks to oversized files are rejected after resolution."""
        import pathlib

        real_file = pathlib.Path(str(tmp_path)) / "huge_real.py"
        real_file.write_text("class Big:\n    pass\n" + "x" * (_MAX_FILE_SIZE_BYTES + 1))
        link = pathlib.Path(str(tmp_path)) / "link_to_huge.py"
        link.symlink_to(real_file)

        assert _extract_public_api(str(link), str(tmp_path)) == []


class TestBuildPublicApiSummary:
    """Tests for _build_public_api_summary."""

    def test_summary_from_multiple_files(self, tmp_path: object) -> None:
        """Test building summary across multiple files."""
        import pathlib

        p1 = pathlib.Path(str(tmp_path)) / "models.py"
        p1.write_text("class User:\n    pass\n\nclass Post:\n    pass\n")
        p2 = pathlib.Path(str(tmp_path)) / "service.py"
        p2.write_text("def get_user(id: str) -> User:\n    pass\n")

        summary = _build_public_api_summary(
            (str(p1), str(p2)),
            workspace_root=str(tmp_path),
        )
        assert "models.py:" in summary
        assert "service.py:" in summary
        assert "class User" in summary
        assert "get_user" in summary

    def test_summary_truncates_long_output(self, tmp_path: object) -> None:
        """Test that summary is truncated to _MAX_PUBLIC_API_CHARS."""
        import pathlib

        p = pathlib.Path(str(tmp_path)) / "big.py"
        lines = [f"def function_{i}(arg: str) -> str:\n    pass\n" for i in range(100)]
        p.write_text("\n".join(lines))

        summary = _build_public_api_summary(
            (str(p),),
            workspace_root=str(tmp_path),
        )
        assert len(summary) <= 500

    def test_summary_skips_missing_files(self, tmp_path: object) -> None:
        """Test that missing files are silently skipped."""
        summary = _build_public_api_summary(
            (str(tmp_path) + "/nonexistent/a.py", str(tmp_path) + "/nonexistent/b.py"),
            workspace_root=str(tmp_path),
        )
        assert summary == ""

    def test_workspace_root_rejects_outside_files(self, tmp_path: object) -> None:
        """Test that files outside workspace_root are skipped."""
        import pathlib
        import tempfile

        workspace = pathlib.Path(str(tmp_path)) / "project"
        workspace.mkdir()
        inside = workspace / "service.py"
        inside.write_text("class InsideService:\n    pass\n")

        # Create a file outside the workspace
        with tempfile.NamedTemporaryFile(suffix=".py", delete=False, mode="w") as f:
            f.write("class OutsideService:\n    pass\n")
            outside_path = f.name

        try:
            summary = _build_public_api_summary(
                (str(inside), outside_path),
                workspace_root=str(workspace),
            )
            assert "InsideService" in summary
            assert "OutsideService" not in summary
        finally:
            pathlib.Path(outside_path).unlink(missing_ok=True)

    def test_workspace_root_allows_inside_files(self, tmp_path: object) -> None:
        """Test that files inside workspace_root are processed normally."""
        import pathlib

        workspace = pathlib.Path(str(tmp_path)) / "project"
        workspace.mkdir()
        f = workspace / "models.py"
        f.write_text("class User:\n    pass\n")

        summary = _build_public_api_summary(
            (str(f),),
            workspace_root=str(workspace),
        )
        assert "class User" in summary

    def test_workspace_root_rejects_symlink_escape(self, tmp_path: object) -> None:
        """Test that symlinks pointing outside workspace are rejected."""
        import pathlib
        import tempfile

        workspace = pathlib.Path(str(tmp_path)) / "project"
        workspace.mkdir()

        # Create a file outside the workspace
        with tempfile.NamedTemporaryFile(suffix=".py", delete=False, mode="w") as f:
            f.write("class EscapedClass:\n    pass\n")
            outside_path = f.name

        # Symlink inside workspace pointing outside
        link = workspace / "sneaky.py"
        link.symlink_to(outside_path)

        try:
            summary = _build_public_api_summary(
                (str(link),),
                workspace_root=str(workspace),
            )
            assert "EscapedClass" not in summary
            assert summary == ""
        finally:
            pathlib.Path(outside_path).unlink(missing_ok=True)

    def test_empty_workspace_root_rejects_all_files(self, tmp_path: object) -> None:
        """Test that an empty workspace_root yields an empty summary.

        Regression guard for the previous ``workspace_root=None`` default
        which silently disabled path containment. The contract is now: no
        workspace, no reads — there is no silent bypass.
        """
        import pathlib

        p = pathlib.Path(str(tmp_path)) / "anywhere.py"
        p.write_text("class Anywhere:\n    pass\n")

        summary = _build_public_api_summary((str(p),), workspace_root="")
        assert summary == ""

    def test_workspace_root_is_required_kwarg(self, tmp_path: object) -> None:
        """Test that workspace_root has no default (callers MUST pass it)."""
        import pathlib

        p = pathlib.Path(str(tmp_path)) / "x.py"
        p.write_text("class X:\n    pass\n")

        with pytest.raises(TypeError):
            # mypy: keep the check narrow — this is a runtime contract guard.
            _build_public_api_summary((str(p),))  # type: ignore[call-arg]

    def test_file_cap_limits_processed_files(self, tmp_path: object) -> None:
        """Test that at most _MAX_FILES_FOR_API files are processed."""
        import pathlib

        workspace = pathlib.Path(str(tmp_path))
        paths: list[str] = []
        for i in range(_MAX_FILES_FOR_API + 5):
            p = workspace / f"mod_{i}.py"
            p.write_text(f"class Class{i}:\n    pass\n")
            paths.append(str(p))

        summary = _build_public_api_summary(
            tuple(paths),
            workspace_root=str(workspace),
        )
        # Count how many distinct "mod_N.py:" entries appear
        import re as _re

        file_entries = _re.findall(r"mod_\d+\.py:", summary)
        assert len(file_entries) <= _MAX_FILES_FOR_API


class TestPromptTextWithPublicApi:
    """Tests for to_prompt_text() showing both public_api and key_output."""

    def test_shows_both_public_api_and_key_output(self) -> None:
        """Test that both public_api and key_output appear in prompt text."""
        ctx = LevelContext(
            level_number=0,
            completed_acs=(
                ACContextSummary(
                    ac_index=0,
                    ac_content="Create user service",
                    success=True,
                    files_modified=("src/service.py",),
                    public_api="service.py: class UserService, def get_user(id: str) -> User",
                    key_output="Service created successfully",
                ),
            ),
        )
        text = ctx.to_prompt_text()
        assert "Public API:" in text
        assert "class UserService" in text
        assert "Result:" in text
        assert "Service created successfully" in text

    def test_shows_only_key_output_when_no_public_api(self) -> None:
        """Test fallback to key_output when public_api is empty."""
        ctx = LevelContext(
            level_number=0,
            completed_acs=(
                ACContextSummary(
                    ac_index=0,
                    ac_content="Run migration",
                    success=True,
                    key_output="Migration applied",
                ),
            ),
        )
        text = ctx.to_prompt_text()
        assert "Public API" not in text
        assert "Result:" in text
        assert "Migration applied" in text
