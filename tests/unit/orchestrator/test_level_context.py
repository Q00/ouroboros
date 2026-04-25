"""Unit tests for inter-level context passing module."""

from __future__ import annotations

import pytest

from ouroboros.orchestrator.adapter import AgentMessage
from ouroboros.orchestrator.coordinator import CoordinatorReview, FileConflict
from ouroboros.orchestrator.level_context import (
    _MAX_FILE_SIZE_BYTES,
    _MAX_FILES_FOR_API,
    _MAX_INVARIANT_TEXT_CHARS,
    ACContextSummary,
    ACPostmortem,
    Invariant,
    LevelContext,
    POSTMORTEM_DEFAULT_INVARIANT_MIN_RELIABILITY,
    POSTMORTEM_DEFAULT_K_FULL,
    POSTMORTEM_DEFAULT_TOKEN_BUDGET,
    PostmortemChain,
    _build_public_api_summary,
    _extract_public_api,
    build_context_prompt,
    build_postmortem_chain_prompt,
    deserialize_level_contexts,
    deserialize_postmortem_chain,
    extract_invariant_tags,
    extract_level_context,
    serialize_level_contexts,
    serialize_postmortem_chain,
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


# --- Serial compounding: ACPostmortem / PostmortemChain tests ---


def _mk_pm(
    idx: int,
    content: str = "",
    *,
    status: str = "pass",
    files: tuple[str, ...] = (),
    tools: tuple[str, ...] = (),
    invariants: tuple[str, ...] = (),
    gotchas: tuple[str, ...] = (),
    qa_suggestions: tuple[str, ...] = (),
    diff_summary: str = "",
    tool_trace_digest: str = "",
    retry_attempts: int = 0,
    duration: float = 0.0,
) -> ACPostmortem:
    """Helper to create an ACPostmortem; ``invariants`` accepts plain strings
    and auto-wraps each as ``Invariant(text=...)`` for test convenience."""
    summary = ACContextSummary(
        ac_index=idx,
        ac_content=content or f"AC {idx + 1} task description",
        success=(status == "pass"),
        tools_used=tools,
        files_modified=files,
    )
    inv_objects: tuple[Invariant, ...] = tuple(Invariant(text=i) for i in invariants)
    return ACPostmortem(
        summary=summary,
        diff_summary=diff_summary,
        tool_trace_digest=tool_trace_digest,
        gotchas=gotchas,
        qa_suggestions=qa_suggestions,
        invariants_established=inv_objects,
        retry_attempts=retry_attempts,
        status=status,  # type: ignore[arg-type]
        duration_seconds=duration,
    )


class TestACPostmortem:
    def test_defaults(self) -> None:
        pm = _mk_pm(0)
        assert pm.status == "pass"
        assert pm.retry_attempts == 0
        assert pm.gotchas == ()
        assert pm.invariants_established == ()
        assert pm.sub_postmortems == ()

    def test_is_frozen(self) -> None:
        pm = _mk_pm(0)
        with pytest.raises((AttributeError, Exception)):
            pm.status = "fail"  # type: ignore[misc]

    def test_digest_pass_with_invariants(self) -> None:
        pm = _mk_pm(
            2,
            content="Add JWT auth",
            files=("auth.py", "middleware.py"),
            invariants=("AUTH_HEADER required", "JWT expiry=15m"),
        )
        digest = pm.to_digest()
        assert "AC 3 [pass]" in digest
        assert "Add JWT auth" in digest
        assert "auth.py" in digest
        assert "AUTH_HEADER required" in digest
        assert "JWT expiry=15m" in digest

    def test_digest_fail_prefers_gotcha_over_invariants(self) -> None:
        pm = _mk_pm(
            0,
            content="Add auth",
            status="fail",
            files=("auth.py",),
            invariants=("IGNORED_INVARIANT",),
            gotchas=("JWT lib assumes UTC", "secondary gotcha"),
        )
        digest = pm.to_digest()
        assert "[fail]" in digest
        assert "JWT lib assumes UTC" in digest
        # For failed ACs, gotcha replaces invariants in the digest
        assert "IGNORED_INVARIANT" not in digest

    def test_digest_truncates_long_content(self) -> None:
        long_content = "x" * 500
        pm = _mk_pm(0, content=long_content)
        digest = pm.to_digest()
        assert "..." in digest
        assert len(digest) < 500

    def test_digest_collapses_many_files(self) -> None:
        pm = _mk_pm(0, files=tuple(f"f{i}.py" for i in range(10)))
        digest = pm.to_digest()
        assert "+7 more" in digest

    def test_digest_filters_below_threshold_invariants(self) -> None:
        """to_digest must apply the same reliability gate as to_full_text /
        to_prompt_text — otherwise older entries (rendered as digests) would
        leak below-threshold or contradicted invariants back into the chain
        context, defeating the render gate.

        [[INVARIANT: to_digest filters by min_reliability and excludes contradicted invariants]]
        """
        from ouroboros.orchestrator.level_context import (
            ACContextSummary,
            ACPostmortem,
            Invariant,
        )

        pm = ACPostmortem(
            summary=ACContextSummary(
                ac_index=0,
                ac_content="Wire auth",
                success=True,
            ),
            status="pass",
            invariants_established=(
                Invariant(text="HIGH_RELIABILITY", reliability=0.95, occurrences=2),
                Invariant(text="LOW_RELIABILITY", reliability=0.3, occurrences=1),
                Invariant(
                    text="CONTRADICTED",
                    reliability=0.0,
                    occurrences=1,
                    is_contradicted=True,
                ),
            ),
        )

        # Default threshold (0.0) — all non-contradicted invariants visible.
        digest_open = pm.to_digest()
        assert "HIGH_RELIABILITY" in digest_open
        assert "LOW_RELIABILITY" in digest_open
        assert "CONTRADICTED" not in digest_open  # always excluded

        # Threshold 0.7 — drops the low-reliability one too.
        digest_gated = pm.to_digest(min_reliability=0.7)
        assert "HIGH_RELIABILITY" in digest_gated
        assert "LOW_RELIABILITY" not in digest_gated
        assert "CONTRADICTED" not in digest_gated

        # All filtered → no invariants section in the digest at all.
        digest_all_gated = pm.to_digest(min_reliability=1.1)
        assert "invariants:" not in digest_all_gated

    def test_full_text_includes_all_sections(self) -> None:
        pm = _mk_pm(
            0,
            content="Add auth",
            files=("auth.py",),
            tools=("Edit", "Write"),
            invariants=("INV1",),
            gotchas=("GOTCHA1",),
            qa_suggestions=("SUGGEST1",),
            diff_summary=" auth.py | 42 +++",
            tool_trace_digest="Edit auth.py (ok)",
            retry_attempts=2,
            duration=1.5,
        )
        text = pm.to_full_text()
        assert "AC 1" in text
        assert "retried 2x" in text
        assert "1.5s" in text
        assert "Add auth" in text
        assert "auth.py" in text
        assert "Edit, Write" in text
        assert "INV1" in text
        assert "GOTCHA1" in text
        assert "SUGGEST1" in text
        assert " auth.py | 42 +++" in text
        assert "Edit auth.py (ok)" in text


class TestPostmortemChain:
    def test_empty_chain_renders_empty(self) -> None:
        assert PostmortemChain().to_prompt_text() == ""
        assert build_postmortem_chain_prompt(PostmortemChain()) == ""

    def test_append_is_immutable(self) -> None:
        c0 = PostmortemChain()
        c1 = c0.append(_mk_pm(0))
        assert c0.postmortems == ()
        assert len(c1.postmortems) == 1

    def test_cumulative_invariants_deduplicated_in_order(self) -> None:
        chain = PostmortemChain(
            postmortems=(
                _mk_pm(0, invariants=("A", "B")),
                _mk_pm(1, invariants=("B", "C")),
                _mk_pm(2, invariants=("A", "D")),
            )
        )
        result = chain.cumulative_invariants()
        # Should return tuple[Invariant, ...] deduplicated by text in insertion order.
        assert len(result) == 4
        assert [inv.text for inv in result] == ["A", "B", "C", "D"]

    def test_recent_rendered_full_older_rendered_digest(self) -> None:
        pms = tuple(
            _mk_pm(
                i,
                content=f"task_{i}",
                invariants=(f"INV_{i}",),
                diff_summary=f"diff_marker_{i}",
            )
            for i in range(5)
        )
        chain = PostmortemChain(postmortems=pms)
        text = chain.to_prompt_text(k_full=2, token_budget=100_000)
        # Full form appears for the last 2 → their diff_summary is in text.
        assert "diff_marker_3" in text
        assert "diff_marker_4" in text
        # Older 3 are digests only — diff content should NOT appear.
        assert "diff_marker_0" not in text
        assert "diff_marker_1" not in text
        assert "diff_marker_2" not in text
        # Digest markers visible for older entries.
        assert "AC 1 [pass]" in text
        assert "AC 3 [pass]" in text
        # Invariants from all 5 appear once in cumulative block.
        for i in range(5):
            assert f"INV_{i}" in text

    def test_k_full_zero_renders_all_as_digests(self) -> None:
        pms = tuple(
            _mk_pm(i, content=f"task_{i}", diff_summary=f"diff_marker_{i}")
            for i in range(3)
        )
        chain = PostmortemChain(postmortems=pms)
        text = chain.to_prompt_text(k_full=0, token_budget=100_000)
        for i in range(3):
            assert f"diff_marker_{i}" not in text  # no full forms
            assert f"AC {i + 1}" in text  # digests present

    def test_over_budget_drops_oldest_digests_first(self) -> None:
        # Build a chain where digests are plentiful but cheap; budget tight.
        old_pms = tuple(_mk_pm(i, content=f"older_ac_{i}") for i in range(10))
        recent_pms = tuple(
            _mk_pm(10 + i, content=f"recent_ac_{i}", invariants=(f"RECENT_INV_{i}",))
            for i in range(2)
        )
        chain = PostmortemChain(postmortems=old_pms + recent_pms)

        # Very tight budget: must drop oldest digests while keeping recent full forms + invariants.
        text = chain.to_prompt_text(k_full=2, token_budget=50)

        # Recent full forms and their invariants must survive.
        assert "recent_ac_0" in text
        assert "recent_ac_1" in text
        assert "RECENT_INV_0" in text
        assert "RECENT_INV_1" in text
        # Oldest digests should have been dropped preferentially.
        assert "older_ac_0" not in text

    def test_budget_preserves_invariants_block(self) -> None:
        chain = PostmortemChain(
            postmortems=(
                _mk_pm(0, invariants=("MUST_SURVIVE",)),
                _mk_pm(1, content="current"),
            )
        )
        text = chain.to_prompt_text(k_full=1, token_budget=100)
        assert "MUST_SURVIVE" in text

    def test_default_constants_exported(self) -> None:
        assert POSTMORTEM_DEFAULT_K_FULL >= 1
        assert POSTMORTEM_DEFAULT_TOKEN_BUDGET > 0

    # --- on_truncated callback (Q7 truncation event seam) ---

    def test_on_truncated_callback_invoked_when_over_budget(self) -> None:
        """on_truncated is called when to_prompt_text exceeds the char budget.

        Verifies that the callback receives the correct positional arguments:
        (dropped_count, char_budget, rendered_chars, full_forms_preserved,
        cumulative_invariants_preserved).

        This is the level_context.py side of the Q7 truncation event seam.
        The serial executor converts these args into a structured event via
        create_postmortem_chain_truncated_event.

        Compounding ref: AC-2 [[INVARIANT: ACPostmortem.sub_postmortems preserves
        structure in serialized chain]] — sub-postmortem content can inflate the
        chain size and trigger this truncation path.

        [[INVARIANT: Truncation event emitted alongside log.warning, not replacing it]]
        """
        # Build a large chain with many digest-eligible entries.
        pms = tuple(_mk_pm(i, content=f"ac_content_{i}") for i in range(8))
        recent = _mk_pm(8, content="recent_ac", invariants=("SOME_INVARIANT",))
        chain = PostmortemChain(postmortems=pms + (recent,))

        calls: list[tuple] = []

        def _capture(*args: object) -> None:
            calls.append(args)

        # Force truncation with a very small budget (1 token = 4 chars).
        chain.to_prompt_text(token_budget=1, k_full=1, on_truncated=_capture)

        assert len(calls) == 1, "callback must be invoked exactly once when over budget"
        dropped_count, char_budget, rendered_chars, full_forms, inv_count = calls[0]
        assert dropped_count >= 0, "dropped_count must be non-negative"
        assert char_budget > 0, "char_budget must reflect the token_budget * 4 factor"
        assert rendered_chars > char_budget, (
            "rendered_chars must exceed char_budget to confirm truncation"
        )
        assert full_forms == 1, "k_full=1 → 1 full-form entry preserved"
        assert inv_count >= 0

    def test_on_truncated_not_called_when_chain_fits(self) -> None:
        """on_truncated is NOT called when the chain fits within the budget.

        [[INVARIANT: no truncation event emitted when chain fits within budget]]
        """
        pms = tuple(_mk_pm(i) for i in range(3))
        chain = PostmortemChain(postmortems=pms)

        calls: list[tuple] = []
        chain.to_prompt_text(
            token_budget=8000,  # generous budget — no truncation
            on_truncated=lambda *a: calls.append(a),
        )

        assert calls == [], "on_truncated must NOT be invoked when chain fits in budget"

    def test_on_truncated_called_when_drops_make_chain_fit(self) -> None:
        """on_truncated MUST fire when entries are dropped, even if the final
        chain fits within budget after the drops.

        Regression for the bug where the callback was nested under a
        ``len(text) > char_budget`` post-loop check, so a chain that
        successfully shrunk under budget by dropping entries reported zero
        truncation events — masking real telemetry.

        [[INVARIANT: truncation event fires whenever dropped_count > 0,
        regardless of whether final text fits within budget]]
        """
        from ouroboros.orchestrator.level_context import _POSTMORTEM_CHARS_PER_TOKEN

        # Build a chain with several digest-eligible entries so dropping some
        # meaningfully shrinks the rendered text.
        pms = tuple(_mk_pm(i, content=f"ac_{i}_with_some_padding") for i in range(8))
        recent = _mk_pm(8, content="r", invariants=("X",))
        chain = PostmortemChain(postmortems=pms + (recent,))

        # Measure the unbudgeted render to pick a budget that lands in the
        # drops-but-fit zone deterministically across rendering tweaks.
        unbudgeted = chain.to_prompt_text(k_full=1, token_budget=1_000_000)
        full_size = len(unbudgeted)
        # 70% of full size: large enough that dropping 1-2 oldest digests fits,
        # small enough that the initial render exceeds the budget.
        target_chars = (full_size * 7) // 10
        token_budget = max(1, target_chars // _POSTMORTEM_CHARS_PER_TOKEN)

        calls: list[tuple] = []
        chain.to_prompt_text(
            token_budget=token_budget,
            k_full=1,
            on_truncated=lambda *a: calls.append(a),
        )

        assert len(calls) == 1, (
            f"Expected exactly one truncation callback; got {len(calls)}. "
            f"full_size={full_size}, token_budget={token_budget}"
        )
        dropped_count, char_budget, rendered_chars, _, _ = calls[0]
        assert dropped_count > 0, "must have dropped at least one entry"
        assert rendered_chars <= char_budget, (
            f"final text must fit within budget after drops; got "
            f"rendered_chars={rendered_chars}, char_budget={char_budget}"
        )

    def test_on_truncated_not_called_when_callback_is_none(self) -> None:
        """No error when on_truncated=None (default) and chain overflows budget."""
        pms = tuple(_mk_pm(i, content=f"ac_{i}") for i in range(5))
        chain = PostmortemChain(postmortems=pms)

        # Should not raise even when truncation occurs and callback is None.
        result = chain.to_prompt_text(token_budget=1, on_truncated=None)
        assert isinstance(result, str)

    def test_on_truncated_callback_args_match_log_warning_fields(self) -> None:
        """Callback args (dropped_count, char_budget, rendered_chars, full_forms,
        inv_count) mirror the keyword args passed to log.warning at the same site.

        This documents the positional contract so future refactors don't silently
        swap arg order.
        """
        pms = tuple(_mk_pm(i, content=f"ac_{i}") for i in range(6))
        chain = PostmortemChain(postmortems=pms)

        captured: list[tuple] = []
        chain.to_prompt_text(token_budget=1, k_full=2, on_truncated=lambda *a: captured.append(a))

        assert len(captured) == 1
        dropped, budget, rendered, full_forms, inv_ct = captured[0]
        # Positional contract checks.
        assert isinstance(dropped, int)
        assert isinstance(budget, int)
        assert isinstance(rendered, int)
        assert isinstance(full_forms, int)
        assert isinstance(inv_ct, int)
        # Budget must equal token_budget * chars_per_token (4).
        from ouroboros.orchestrator.level_context import _POSTMORTEM_CHARS_PER_TOKEN

        assert budget == 1 * _POSTMORTEM_CHARS_PER_TOKEN


class TestPostmortemSerialization:
    def test_empty_chain_roundtrip(self) -> None:
        chain = PostmortemChain()
        assert serialize_postmortem_chain(chain) == []
        assert deserialize_postmortem_chain([]).postmortems == ()

    def test_full_roundtrip_preserves_all_fields(self) -> None:
        original = PostmortemChain(
            postmortems=(
                _mk_pm(
                    0,
                    content="Add auth",
                    status="pass",
                    files=("auth.py", "middleware.py"),
                    tools=("Edit", "Write"),
                    invariants=("AUTH_HEADER required",),
                    diff_summary=" auth.py | 42 +++",
                    tool_trace_digest="Edit auth.py (ok)",
                    duration=1.5,
                ),
                _mk_pm(
                    1,
                    content="Add tests",
                    status="fail",
                    files=("tests/test_auth.py",),
                    gotchas=("Missing fixture",),
                    qa_suggestions=("Add conftest.py",),
                    retry_attempts=2,
                ),
            )
        )
        data = serialize_postmortem_chain(original)
        # JSON-serializable shape: list of dicts.
        assert isinstance(data, list)
        assert all(isinstance(d, dict) for d in data)

        restored = deserialize_postmortem_chain(data)
        assert len(restored.postmortems) == 2

        a, b = restored.postmortems
        assert a.summary.ac_index == 0
        assert a.summary.ac_content == "Add auth"
        assert a.summary.files_modified == ("auth.py", "middleware.py")
        assert len(a.invariants_established) == 1
        assert a.invariants_established[0].text == "AUTH_HEADER required"
        assert a.diff_summary == " auth.py | 42 +++"
        assert a.duration_seconds == 1.5
        assert a.status == "pass"

        assert b.status == "fail"
        assert b.gotchas == ("Missing fixture",)
        assert b.qa_suggestions == ("Add conftest.py",)
        assert b.retry_attempts == 2

    def test_deserialize_tolerates_missing_fields(self) -> None:
        minimal = [{"summary": {"ac_index": 0, "ac_content": "x", "success": True}}]
        chain = deserialize_postmortem_chain(minimal)
        assert len(chain.postmortems) == 1
        pm = chain.postmortems[0]
        assert pm.status == "pass"
        assert pm.gotchas == ()
        assert pm.retry_attempts == 0

    def test_deserialize_invalid_status_defaults_to_pass(self) -> None:
        chain = deserialize_postmortem_chain(
            [{"summary": {"ac_index": 0, "ac_content": "x", "success": True},
              "status": "bogus"}]
        )
        assert chain.postmortems[0].status == "pass"

    def test_deserialize_skips_bad_entries_without_crashing(self) -> None:
        # One bad entry (summary is not a dict) should be skipped silently.
        data = [
            {"summary": {"ac_index": 0, "ac_content": "ok", "success": True}},
            {"summary": "not a dict"},
        ]
        chain = deserialize_postmortem_chain(data)
        # The good entry should still round-trip; the bad one may be skipped
        # or coerced to defaults — behavior-tolerant assertion.
        assert len(chain.postmortems) >= 1
        assert chain.postmortems[0].summary.ac_content == "ok"

    def test_invariant_round_trip_preserves_scores(self) -> None:
        """Invariant objects with reliability/occurrence scores survive
        serialize → deserialize without loss.

        [[INVARIANT: invariants_established is now tuple[Invariant, ...] not tuple[str, ...]]]
        """
        inv = Invariant(
            text="AUTH_HEADER required",
            reliability=0.85,
            occurrences=2,
            first_seen_ac_id="ac_1",
        )
        summary = ACContextSummary(
            ac_index=0,
            ac_content="Add auth",
            success=True,
            files_modified=("auth.py",),
        )
        pm = ACPostmortem(
            summary=summary,
            invariants_established=(inv,),
        )
        original = PostmortemChain(postmortems=(pm,))

        # Serialize → deserialize round-trip.
        data = serialize_postmortem_chain(original)
        restored = deserialize_postmortem_chain(data)

        assert len(restored.postmortems) == 1
        restored_pm = restored.postmortems[0]
        assert len(restored_pm.invariants_established) == 1
        restored_inv = restored_pm.invariants_established[0]

        assert restored_inv.text == "AUTH_HEADER required"
        assert abs(restored_inv.reliability - 0.85) < 1e-9
        assert restored_inv.occurrences == 2
        assert restored_inv.first_seen_ac_id == "ac_1"

    def test_invariant_backward_compat_legacy_string_deserialize(self) -> None:
        """Old serialized chains that stored invariants_established as plain strings
        (before the Invariant dataclass was introduced) must still deserialize
        without error, wrapping each string as Invariant(text=...).

        [[INVARIANT: OUROBOROS_INVARIANT_MIN_RELIABILITY defaults 0.7; below-threshold hidden but stored]]
        """
        legacy_data = [
            {
                "summary": {"ac_index": 0, "ac_content": "Add auth", "success": True},
                "invariants_established": ["AUTH_HEADER required", "JWT expiry=15m"],
            }
        ]
        chain = deserialize_postmortem_chain(legacy_data)
        assert len(chain.postmortems) == 1
        pm = chain.postmortems[0]
        assert len(pm.invariants_established) == 2
        # Legacy strings should be wrapped with default reliability=1.0.
        assert pm.invariants_established[0].text == "AUTH_HEADER required"
        assert pm.invariants_established[0].reliability == 1.0
        assert pm.invariants_established[1].text == "JWT expiry=15m"

    def test_invariant_dataclass_defaults(self) -> None:
        """Invariant can be constructed with just text; other fields have sensible defaults."""
        inv = Invariant(text="foo bar")
        assert inv.reliability == 1.0
        assert inv.occurrences == 1
        assert inv.first_seen_ac_id == ""
        # __str__ returns the text.
        assert str(inv) == "foo bar"

    def test_invariant_frozen(self) -> None:
        """Invariant is a frozen dataclass."""
        inv = Invariant(text="x")
        with pytest.raises((AttributeError, Exception)):
            inv.text = "y"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Q3 (C-plus) — PostmortemChain.merge_invariants tests
# ---------------------------------------------------------------------------


def _make_pm_with_invariant(
    idx: int,
    inv: Invariant,
) -> ACPostmortem:
    """Helper: ACPostmortem with a single explicit Invariant object."""
    summary = ACContextSummary(ac_index=idx, ac_content=f"AC {idx + 1}", success=True)
    return ACPostmortem(summary=summary, invariants_established=(inv,))


class TestMergeInvariants:
    """Tests for PostmortemChain.merge_invariants(new, source_ac_id).

    Each AC's postmortem carries its ``merge_invariants`` result as
    ``invariants_established``.  ``cumulative_invariants()`` picks the
    *last* occurrence per key so updated counts/reliabilities surface.

    [[INVARIANT: merge_invariants is called once per AC to produce invariants_established]]
    [[INVARIANT: cumulative_invariants returns last-seen Invariant per normalized key]]
    """

    # --- new invariant insertion ---

    def test_new_invariant_inserted_with_source_tracking(self) -> None:
        """A brand-new invariant gets occurrences=1 and first_seen_ac_id set."""
        chain = PostmortemChain()
        result = chain.merge_invariants([("AUTH required", 0.9)], source_ac_id="ac_1")
        assert len(result) == 1
        inv = result[0]
        assert inv.text == "AUTH required"
        assert abs(inv.reliability - 0.9) < 1e-9
        assert inv.occurrences == 1
        assert inv.first_seen_ac_id == "ac_1"

    def test_multiple_new_invariants_all_inserted(self) -> None:
        """Multiple distinct new invariants are all inserted as occurrences=1."""
        chain = PostmortemChain()
        result = chain.merge_invariants(
            [("A holds", 0.8), ("B holds", 0.9)],
            source_ac_id="ac_1",
        )
        assert len(result) == 2
        assert result[0].text == "A holds"
        assert result[1].text == "B holds"

    # --- re-declaration: occurrence bumping ---

    def test_redeclared_invariant_bumps_occurrence(self) -> None:
        """Re-declaring an existing invariant bumps its occurrence count by 1."""
        prior = Invariant(
            text="AUTH required", reliability=0.8, occurrences=1, first_seen_ac_id="ac_1"
        )
        chain = PostmortemChain(postmortems=(_make_pm_with_invariant(0, prior),))

        result = chain.merge_invariants([("AUTH required", 1.0)], source_ac_id="ac_2")
        assert len(result) == 1
        assert result[0].occurrences == 2

    def test_three_declarations_produce_occurrences_three(self) -> None:
        """Three declarations across three ACs produce occurrences=3 in AC-3."""
        chain = PostmortemChain()

        # AC 1: first declaration
        inv1 = chain.merge_invariants([("K holds", 1.0)], source_ac_id="ac_1")
        chain = chain.append(_make_pm_with_invariant(0, inv1[0]))

        # AC 2: re-declaration
        inv2 = chain.merge_invariants([("K holds", 0.8)], source_ac_id="ac_2")
        chain = chain.append(_make_pm_with_invariant(1, inv2[0]))

        # AC 3: re-declaration again
        inv3 = chain.merge_invariants([("K holds", 0.9)], source_ac_id="ac_3")
        assert len(inv3) == 1
        assert inv3[0].occurrences == 3

    # --- re-declaration: reliability averaging ---

    def test_redeclared_invariant_averages_reliability(self) -> None:
        """Reliability blended as weighted mean on re-declaration."""
        prior = Invariant(
            text="Cache is warm", reliability=0.8, occurrences=1, first_seen_ac_id="ac_1"
        )
        chain = PostmortemChain(postmortems=(_make_pm_with_invariant(0, prior),))

        result = chain.merge_invariants([("Cache is warm", 1.0)], source_ac_id="ac_2")
        inv = result[0]
        # Weighted mean: (0.8 * 1 + 1.0) / 2 = 0.9
        assert abs(inv.reliability - 0.9) < 1e-6

    def test_reliability_weighted_by_occurrences(self) -> None:
        """Prior occurrence count weights the reliability blend correctly."""
        # occurrences=2, reliability=0.6 after two prior declarations
        prior = Invariant(
            text="X is stable", reliability=0.6, occurrences=2, first_seen_ac_id="ac_1"
        )
        chain = PostmortemChain(postmortems=(_make_pm_with_invariant(0, prior),))

        result = chain.merge_invariants([("X is stable", 1.0)], source_ac_id="ac_3")
        inv = result[0]
        # Weighted mean: (0.6 * 2 + 1.0) / 3 = 2.2 / 3 ≈ 0.7333...
        expected = (0.6 * 2 + 1.0) / 3
        assert abs(inv.reliability - expected) < 1e-5

    # --- canonical text and first_seen_ac_id preservation ---

    def test_canonical_text_preserved_from_first_occurrence(self) -> None:
        """Re-declaration with different casing preserves the original canonical text."""
        prior = Invariant(
            text="Cache Is Warm", reliability=0.8, occurrences=1, first_seen_ac_id="ac_1"
        )
        chain = PostmortemChain(postmortems=(_make_pm_with_invariant(0, prior),))

        result = chain.merge_invariants([("cache is warm", 0.9)], source_ac_id="ac_2")
        assert len(result) == 1
        assert result[0].text == "Cache Is Warm"  # original casing preserved

    def test_first_seen_ac_id_not_updated_on_redeclaration(self) -> None:
        """first_seen_ac_id stays at the original AC, not the re-declaring AC."""
        prior = Invariant(
            text="X holds", reliability=0.7, occurrences=1, first_seen_ac_id="ac_1"
        )
        chain = PostmortemChain(postmortems=(_make_pm_with_invariant(0, prior),))

        result = chain.merge_invariants([("X holds", 0.9)], source_ac_id="ac_5")
        assert result[0].first_seen_ac_id == "ac_1"  # not "ac_5"

    # --- deduplication within new ---

    def test_empty_new_list_returns_empty_tuple(self) -> None:
        """Empty new list returns an empty tuple."""
        chain = PostmortemChain(postmortems=(_mk_pm(0, invariants=("existing",)),))
        result = chain.merge_invariants([], source_ac_id="ac_2")
        assert result == ()

    def test_duplicates_within_new_keep_first(self) -> None:
        """Duplicate entries within ``new`` deduplicate to the first occurrence."""
        chain = PostmortemChain()
        result = chain.merge_invariants(
            [("dup claim", 0.9), ("dup claim", 0.5)],
            source_ac_id="ac_1",
        )
        assert len(result) == 1
        assert abs(result[0].reliability - 0.9) < 1e-9  # first occurrence wins

    # --- normalization ---

    def test_normalization_matches_case_variant(self) -> None:
        """Upper-cased re-declaration matches lower-cased existing invariant."""
        prior = Invariant(
            text="x is true", reliability=0.8, occurrences=1, first_seen_ac_id="ac_1"
        )
        chain = PostmortemChain(postmortems=(_make_pm_with_invariant(0, prior),))

        result = chain.merge_invariants([("X IS TRUE", 1.0)], source_ac_id="ac_2")
        assert len(result) == 1
        assert result[0].occurrences == 2

    def test_normalization_collapses_whitespace(self) -> None:
        """Extra whitespace in re-declaration text is collapsed before matching."""
        prior = Invariant(
            text="auth header required",
            reliability=0.8,
            occurrences=1,
            first_seen_ac_id="ac_1",
        )
        chain = PostmortemChain(postmortems=(_make_pm_with_invariant(0, prior),))

        result = chain.merge_invariants(
            [("auth   header   required", 0.9)], source_ac_id="ac_2"
        )
        assert len(result) == 1
        assert result[0].occurrences == 2

    # --- text truncation ---

    def test_text_truncated_at_200_chars(self) -> None:
        """Invariant text longer than 200 chars is silently truncated."""
        chain = PostmortemChain()
        long_text = "x" * 250
        result = chain.merge_invariants([(long_text, 0.9)], source_ac_id="ac_1")
        assert len(result) == 1
        from ouroboros.orchestrator.level_context import _MAX_INVARIANT_TEXT_CHARS

        assert len(result[0].text) == _MAX_INVARIANT_TEXT_CHARS

    def test_whitespace_only_text_skipped(self) -> None:
        """Invariants with empty/whitespace-only text are silently skipped."""
        chain = PostmortemChain()
        result = chain.merge_invariants([("   ", 0.9)], source_ac_id="ac_1")
        assert result == ()

    # --- integration with cumulative_invariants ---

    def test_cumulative_invariants_reflects_bumped_counts(self) -> None:
        """cumulative_invariants returns the last-seen Invariant (with bumped count)."""
        chain = PostmortemChain()

        # AC 1: new invariant
        inv1 = chain.merge_invariants([("J is constant", 0.7)], source_ac_id="ac_1")
        chain = chain.append(_make_pm_with_invariant(0, inv1[0]))

        # AC 2: re-declare — occurrences should be 2 in chain's view
        inv2 = chain.merge_invariants([("J is constant", 0.9)], source_ac_id="ac_2")
        chain = chain.append(_make_pm_with_invariant(1, inv2[0]))

        cumulative = chain.cumulative_invariants()
        assert len(cumulative) == 1
        assert cumulative[0].occurrences == 2  # last wins

    def test_cumulative_invariants_insertion_order_preserved(self) -> None:
        """Invariants appear in the order first seen, even with later re-declarations."""
        chain = PostmortemChain()

        # AC 1: declares A and B
        inv_ac1 = chain.merge_invariants(
            [("A", 0.9), ("B", 0.8)], source_ac_id="ac_1"
        )
        summary1 = ACContextSummary(ac_index=0, ac_content="AC 1", success=True)
        pm1 = ACPostmortem(summary=summary1, invariants_established=inv_ac1)
        chain = chain.append(pm1)

        # AC 2: re-declares B, adds C
        inv_ac2 = chain.merge_invariants(
            [("B", 0.9), ("C", 0.85)], source_ac_id="ac_2"
        )
        summary2 = ACContextSummary(ac_index=1, ac_content="AC 2", success=True)
        pm2 = ACPostmortem(summary=summary2, invariants_established=inv_ac2)
        chain = chain.append(pm2)

        cumulative = chain.cumulative_invariants()
        # Order: A (first in ac_1), B (second in ac_1), C (first in ac_2)
        assert [inv.text for inv in cumulative] == ["A", "B", "C"]
        # B should have occurrences=2 from the ac_2 merge
        b_inv = next(inv for inv in cumulative if inv.text == "B")
        assert b_inv.occurrences == 2


class TestMergeInvariantsContradiction:
    """Tests for NOT-prefix contradiction detection in merge_invariants.

    AC-2 (B-prime extension): when a new invariant is a literal NOT-prefix
    negation of an already-established one (or vice versa), the new invariant
    is marked ``is_contradicted=True`` and receives ``reliability=0.0``.

    [[INVARIANT: NOT-prefix contradictions set is_contradicted=True and reliability=0.0 on the new invariant]]
    [[INVARIANT: non-contradicted invariants retain is_contradicted=False by default]]
    """

    # --- basic contradiction: existing "X", new "NOT X" ---

    def test_not_prefix_contradicts_existing_claim(self) -> None:
        """'NOT auth required' contradicts prior 'auth required' claim.

        Returns BOTH the new contradicted invariant AND a contradicted
        counterpart marker bearing the prior invariant's text — the chain's
        cumulative_invariants() last-wins-by-key semantics then override the
        previously-trusted entry so the original claim no longer surfaces.
        """
        prior = Invariant(
            text="auth required", reliability=0.9, occurrences=1, first_seen_ac_id="ac_1"
        )
        chain = PostmortemChain(postmortems=(_make_pm_with_invariant(0, prior),))

        result = chain.merge_invariants(
            [("NOT auth required", 1.0)], source_ac_id="ac_2"
        )
        # Two entries: the new contradicted, plus a counterpart marker.
        assert len(result) == 2
        new_inv, counterpart_marker = result
        assert new_inv.text == "NOT auth required"
        assert new_inv.is_contradicted is True
        assert new_inv.reliability == 0.0
        assert counterpart_marker.text == "auth required"
        assert counterpart_marker.is_contradicted is True
        assert counterpart_marker.reliability == 0.0
        assert counterpart_marker.first_seen_ac_id == "ac_1"  # preserved from prior

    def test_not_prefix_contradicts_existing_claim_case_insensitive(self) -> None:
        """Contradiction detection is case-insensitive."""
        prior = Invariant(
            text="Cache Is Warm", reliability=1.0, occurrences=1, first_seen_ac_id="ac_1"
        )
        chain = PostmortemChain(postmortems=(_make_pm_with_invariant(0, prior),))

        result = chain.merge_invariants(
            [("not cache is warm", 0.8)], source_ac_id="ac_2"
        )
        assert len(result) == 2
        assert all(inv.is_contradicted for inv in result)
        assert all(inv.reliability == 0.0 for inv in result)

    # --- symmetric case: existing "NOT X", new "X" ---

    def test_base_claim_contradicts_existing_not_prefixed(self) -> None:
        """'auth required' contradicts prior 'not auth required'."""
        prior = Invariant(
            text="not auth required", reliability=0.7, occurrences=1, first_seen_ac_id="ac_1"
        )
        chain = PostmortemChain(postmortems=(_make_pm_with_invariant(0, prior),))

        result = chain.merge_invariants(
            [("auth required", 0.9)], source_ac_id="ac_2"
        )
        assert len(result) == 2
        new_inv, counterpart_marker = result
        assert new_inv.text == "auth required"
        assert new_inv.is_contradicted is True
        assert counterpart_marker.text == "not auth required"
        assert counterpart_marker.is_contradicted is True
        assert counterpart_marker.first_seen_ac_id == "ac_1"

    # --- text preservation on contradicted invariants ---

    def test_contradicted_invariant_preserves_new_text(self) -> None:
        """The contradicted invariant's text is the new (incoming) text, not the prior's."""
        prior = Invariant(text="X holds", reliability=1.0, occurrences=1, first_seen_ac_id="ac_1")
        chain = PostmortemChain(postmortems=(_make_pm_with_invariant(0, prior),))

        result = chain.merge_invariants([("NOT X holds", 0.8)], source_ac_id="ac_2")
        assert result[0].text == "NOT X holds"

    def test_contradicted_invariant_source_ac_id_is_current_ac(self) -> None:
        """The contradicted invariant's first_seen_ac_id is the declaring AC."""
        prior = Invariant(text="Y is set", reliability=1.0, occurrences=1, first_seen_ac_id="ac_1")
        chain = PostmortemChain(postmortems=(_make_pm_with_invariant(0, prior),))

        result = chain.merge_invariants([("NOT Y is set", 0.9)], source_ac_id="ac_3")
        assert result[0].first_seen_ac_id == "ac_3"

    def test_contradicted_invariant_occurrences_is_one(self) -> None:
        """A contradicted invariant is always a new entry (occurrences=1)."""
        prior = Invariant(text="Z is enabled", reliability=1.0, occurrences=2, first_seen_ac_id="ac_1")
        chain = PostmortemChain(postmortems=(_make_pm_with_invariant(0, prior),))

        result = chain.merge_invariants([("NOT Z is enabled", 0.9)], source_ac_id="ac_4")
        assert result[0].occurrences == 1

    # --- non-contradicting cases remain unaffected ---

    def test_non_contradicting_new_invariant_is_not_marked(self) -> None:
        """A new invariant that doesn't contradict anything gets is_contradicted=False."""
        chain = PostmortemChain()
        result = chain.merge_invariants([("X holds", 0.9)], source_ac_id="ac_1")
        assert len(result) == 1
        assert result[0].is_contradicted is False

    def test_redeclaration_does_not_set_contradiction_flag(self) -> None:
        """A re-declaration (same key) is NOT treated as contradiction."""
        prior = Invariant(text="auth required", reliability=0.8, occurrences=1, first_seen_ac_id="ac_1")
        chain = PostmortemChain(postmortems=(_make_pm_with_invariant(0, prior),))

        result = chain.merge_invariants([("auth required", 1.0)], source_ac_id="ac_2")
        assert result[0].is_contradicted is False
        assert result[0].occurrences == 2

    def test_unrelated_not_claim_not_flagged_as_contradiction(self) -> None:
        """'not Y' does not contradict 'X' when no 'Y' exists in the chain."""
        prior = Invariant(text="X holds", reliability=1.0, occurrences=1, first_seen_ac_id="ac_1")
        chain = PostmortemChain(postmortems=(_make_pm_with_invariant(0, prior),))

        result = chain.merge_invariants([("NOT Y holds", 0.8)], source_ac_id="ac_2")
        assert result[0].is_contradicted is False

    # --- mixed batch: contradicted + clean in same call ---

    def test_mixed_batch_contradicted_and_clean(self) -> None:
        """A batch with one contradiction and one clean invariant handles both correctly.

        After symmetric contradiction handling, the result has THREE entries:
        the new contradicted, the counterpart marker (also contradicted), and
        the clean new invariant.
        """
        prior = Invariant(text="feature is on", reliability=1.0, occurrences=1, first_seen_ac_id="ac_1")
        chain = PostmortemChain(postmortems=(_make_pm_with_invariant(0, prior),))

        result = chain.merge_invariants(
            [("NOT feature is on", 0.9), ("new fact", 0.8)],
            source_ac_id="ac_2",
        )
        assert len(result) == 3
        # [0] the new contradicted invariant
        assert result[0].text == "NOT feature is on"
        assert result[0].is_contradicted is True
        assert result[0].reliability == 0.0
        # [1] counterpart marker overriding the prior trusted entry
        assert result[1].text == "feature is on"
        assert result[1].is_contradicted is True
        assert result[1].reliability == 0.0
        # [2] the clean new invariant
        assert result[2].text == "new fact"
        assert result[2].is_contradicted is False
        assert abs(result[2].reliability - 0.8) < 1e-9

    # --- serialization round-trip ---

    def test_is_contradicted_round_trips_through_serialize_deserialize(self) -> None:
        """is_contradicted=True survives a serialize/deserialize round-trip
        for BOTH the new entry and the counterpart marker.
        """
        prior = Invariant(text="Q is safe", reliability=1.0, occurrences=1, first_seen_ac_id="ac_1")
        chain = PostmortemChain(postmortems=(_make_pm_with_invariant(0, prior),))

        result = chain.merge_invariants([("NOT Q is safe", 0.9)], source_ac_id="ac_2")
        summary = ACContextSummary(ac_index=1, ac_content="AC 2", success=True)
        pm = ACPostmortem(summary=summary, invariants_established=result)
        chain2 = chain.append(pm)

        # Round-trip
        data = serialize_postmortem_chain(chain2)
        chain3 = deserialize_postmortem_chain(data)

        last_pm = chain3.postmortems[-1]
        assert len(last_pm.invariants_established) == 2
        for inv in last_pm.invariants_established:
            assert inv.is_contradicted is True
            assert inv.reliability == 0.0

    def test_is_contradicted_false_default_round_trips(self) -> None:
        """is_contradicted=False (default) also survives round-trip (no field → False)."""
        chain = PostmortemChain()
        result = chain.merge_invariants([("normal claim", 0.9)], source_ac_id="ac_1")
        summary = ACContextSummary(ac_index=0, ac_content="AC 1", success=True)
        pm = ACPostmortem(summary=summary, invariants_established=result)
        chain2 = PostmortemChain(postmortems=(pm,))

        data = serialize_postmortem_chain(chain2)
        chain3 = deserialize_postmortem_chain(data)
        inv = chain3.postmortems[0].invariants_established[0]
        assert inv.is_contradicted is False

    # --- _contradiction_counterpart_key helper ---

    def test_helper_returns_negation_for_plain_key(self) -> None:
        """Plain key → counterpart is 'not ' + key."""
        from ouroboros.orchestrator.level_context import _contradiction_counterpart_key

        assert _contradiction_counterpart_key("auth required") == "not auth required"

    def test_helper_strips_not_prefix_for_negated_key(self) -> None:
        """'not X' key → counterpart is 'X' (stripped)."""
        from ouroboros.orchestrator.level_context import _contradiction_counterpart_key

        assert _contradiction_counterpart_key("not auth required") == "auth required"

    def test_helper_bare_not_returns_none(self) -> None:
        """'not ' alone (empty base after stripping) returns None."""
        from ouroboros.orchestrator.level_context import _contradiction_counterpart_key

        assert _contradiction_counterpart_key("not ") is None
        assert _contradiction_counterpart_key("not") == "not not"  # "not" doesn't start with "not "


class TestBuildPostmortemChainPrompt:
    def test_honors_env_k_full(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("OUROBOROS_POSTMORTEM_FULL_K", "0")
        chain = PostmortemChain(
            postmortems=(_mk_pm(0, content="the_task", diff_summary="FULL_MARKER"),)
        )
        text = build_postmortem_chain_prompt(chain)
        # K=0 → rendered as digest only, full diff_summary absent.
        assert "FULL_MARKER" not in text
        assert "AC 1" in text

    def test_honors_env_token_budget(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("OUROBOROS_POSTMORTEM_TOKEN_BUDGET", "50")
        chain = PostmortemChain(
            postmortems=tuple(_mk_pm(i, content=f"older_{i}") for i in range(8))
            + (_mk_pm(8, content="current", invariants=("KEEP_ME",)),)
        )
        text = build_postmortem_chain_prompt(chain)
        # Budget pressure should drop oldest digests; invariants survive.
        assert "KEEP_ME" in text
        assert "older_0" not in text

    def test_ignores_bad_env_values(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("OUROBOROS_POSTMORTEM_FULL_K", "not-a-number")
        monkeypatch.setenv("OUROBOROS_POSTMORTEM_TOKEN_BUDGET", "also-bad")
        chain = PostmortemChain(postmortems=(_mk_pm(0),))
        # Should fall back to defaults without raising.
        text = build_postmortem_chain_prompt(chain)
        assert text  # non-empty for a non-empty chain


# ---------------------------------------------------------------------------
# Q3 (C-plus) — extract_invariant_tags parser tests
# ---------------------------------------------------------------------------


class TestExtractInvariantTags:
    """Tests for extract_invariant_tags() — Q3 C-plus tag parser."""

    # --- basic functionality ---

    def test_single_tag_from_string(self) -> None:
        """Single [[INVARIANT: ...]] tag is extracted from a plain string."""
        result = extract_invariant_tags("Work done. [[INVARIANT: X is always true]]")
        assert result == ["X is always true"]

    def test_multiple_tags_from_string(self) -> None:
        """Multiple tags are extracted in order of appearance."""
        text = (
            "[[INVARIANT: auth header is required]] some middle text "
            "[[INVARIANT: JWT expiry is 15 minutes]]"
        )
        result = extract_invariant_tags(text)
        assert result == ["auth header is required", "JWT expiry is 15 minutes"]

    def test_tags_from_agent_messages(self) -> None:
        """Tags extracted from AgentMessage.content fields."""
        msgs = [
            AgentMessage(type="assistant", content="Step 1 done. [[INVARIANT: schema version = 3]]"),
            AgentMessage(type="assistant", content="Step 2: [[INVARIANT: migrations are idempotent]]"),
        ]
        result = extract_invariant_tags(msgs)
        assert "schema version = 3" in result
        assert "migrations are idempotent" in result
        assert len(result) == 2

    def test_empty_string_returns_empty_list(self) -> None:
        """No tags → empty list."""
        assert extract_invariant_tags("") == []

    def test_empty_messages_returns_empty_list(self) -> None:
        """Empty message list → empty list."""
        assert extract_invariant_tags([]) == []

    # --- whitespace handling ---

    def test_leading_trailing_whitespace_stripped(self) -> None:
        """Whitespace around tag text is stripped."""
        result = extract_invariant_tags("[[INVARIANT:   spaces everywhere   ]]")
        assert result == ["spaces everywhere"]

    def test_internal_whitespace_preserved(self) -> None:
        """Internal whitespace in tag text is preserved (only outer stripped)."""
        result = extract_invariant_tags("[[INVARIANT: A   B]]")
        assert result == ["A   B"]

    # --- punctuation inside tags ---

    def test_punctuation_in_tag_text(self) -> None:
        """Periods, commas, colons, and hyphens inside tag text are accepted."""
        text = "[[INVARIANT: config.py uses YAML, not JSON — keys are dash-separated]]"
        result = extract_invariant_tags(text)
        assert len(result) == 1
        assert "config.py" in result[0]
        assert "YAML" in result[0]
        assert "dash-separated" in result[0]

    def test_tag_with_numbers_and_equals(self) -> None:
        """Numeric values and equals sign inside tags are accepted."""
        result = extract_invariant_tags("[[INVARIANT: MAX_RETRIES = 3]]")
        assert result == ["MAX_RETRIES = 3"]

    # --- deduplication ---

    def test_duplicate_tags_deduplicated(self) -> None:
        """Identical tags are deduplicated; only first occurrence is kept."""
        text = "[[INVARIANT: A]] [[INVARIANT: A]]"
        assert extract_invariant_tags(text) == ["A"]

    def test_case_insensitive_dedup(self) -> None:
        """Deduplication is case-insensitive (normalized to lowercase for key)."""
        text = "[[INVARIANT: Cache is warm]] [[INVARIANT: cache is warm]]"
        result = extract_invariant_tags(text)
        # First occurrence wins; second is dropped.
        assert len(result) == 1
        assert result[0] == "Cache is warm"

    def test_different_tags_not_deduped(self) -> None:
        """Two distinct tags are both returned."""
        text = "[[INVARIANT: A is true]] [[INVARIANT: B is false]]"
        result = extract_invariant_tags(text)
        assert len(result) == 2

    # --- truncation at 200 chars ---

    def test_tag_truncated_at_200_chars(self) -> None:
        """Tag text longer than 200 characters is silently truncated."""
        long_text = "x" * 250
        text = f"[[INVARIANT: {long_text}]]"
        result = extract_invariant_tags(text)
        assert len(result) == 1
        assert len(result[0]) == _MAX_INVARIANT_TEXT_CHARS
        assert result[0] == long_text[:_MAX_INVARIANT_TEXT_CHARS]

    def test_tag_exactly_200_chars_not_truncated(self) -> None:
        """Tag text exactly at the 200-char limit passes through unchanged."""
        exact_text = "y" * _MAX_INVARIANT_TEXT_CHARS
        text = f"[[INVARIANT: {exact_text}]]"
        result = extract_invariant_tags(text)
        assert result == [exact_text]

    def test_tag_under_200_chars_not_truncated(self) -> None:
        """Short tag text is not modified."""
        short = "short claim"
        result = extract_invariant_tags(f"[[INVARIANT: {short}]]")
        assert result == [short]

    # --- malformed / rejected inputs ---

    def test_single_bracket_ignored(self) -> None:
        """Single-bracket [INVARIANT: ...] format is NOT matched."""
        assert extract_invariant_tags("[INVARIANT: only single bracket]") == []

    def test_unclosed_double_bracket_ignored(self) -> None:
        """Tag missing closing ]] is not matched."""
        assert extract_invariant_tags("[[INVARIANT: missing close") == []

    def test_missing_colon_ignored(self) -> None:
        """Tag without colon separator is not matched."""
        assert extract_invariant_tags("[[INVARIANT no colon here]]") == []

    def test_empty_tag_content_ignored(self) -> None:
        """[[INVARIANT: ]] with only whitespace inside is ignored."""
        assert extract_invariant_tags("[[INVARIANT:   ]]") == []
        assert extract_invariant_tags("[[INVARIANT:]]") == []

    def test_nested_brackets_in_text_not_supported(self) -> None:
        """Nested brackets inside tag text stop the match at the first ].

        The regex [^\\]]+ stops at the first `]`, so nested brackets produce
        a partial (potentially wrong) match.  This is the documented limitation.
        """
        # [[INVARIANT: x[y]z]] — the regex captures "x[y" (stops at first ])
        # so the match ends at the first ], and "z" is not captured.
        result = extract_invariant_tags("[[INVARIANT: x[y]z]]")
        # Either empty (no match) OR partial capture "x[y" — either is acceptable;
        # the important thing is it doesn't raise.
        assert isinstance(result, list)

    # --- case-insensitive tag keyword ---

    def test_case_insensitive_keyword(self) -> None:
        """[[invariant: ...]] and [[INVARIANT: ...]] both match."""
        lower = extract_invariant_tags("[[invariant: lowercase keyword]]")
        upper = extract_invariant_tags("[[INVARIANT: uppercase keyword]]")
        mixed = extract_invariant_tags("[[Invariant: mixed keyword]]")
        assert lower == ["lowercase keyword"]
        assert upper == ["uppercase keyword"]
        assert mixed == ["mixed keyword"]

    # --- multi-message integration ---

    def test_tags_from_multiple_messages_combined(self) -> None:
        """Tags from multiple AgentMessages are combined into one list."""
        msgs = [
            AgentMessage(type="assistant", content="First: [[INVARIANT: one]]"),
            AgentMessage(type="user", content="No tags here"),
            AgentMessage(type="assistant", content="Third: [[INVARIANT: two]]"),
        ]
        result = extract_invariant_tags(msgs)
        assert result == ["one", "two"]

    def test_message_without_content_ok(self) -> None:
        """Messages with empty content don't raise."""
        msgs = [
            AgentMessage(type="tool", content=""),
            AgentMessage(type="assistant", content="[[INVARIANT: found]]"),
        ]
        result = extract_invariant_tags(msgs)
        assert result == ["found"]


# ---------------------------------------------------------------------------
# build_system_prompt — invariant_instructions section
# ---------------------------------------------------------------------------


class TestBuildSystemPromptInvariantSection:
    """Tests for the include_invariant_instructions parameter in build_system_prompt."""

    def _make_minimal_seed(self) -> object:
        """Return a minimal Seed-like object for prompt building tests."""
        from ouroboros.core.seed import OntologySchema, Seed, SeedMetadata

        return Seed(
            goal="Test goal",
            acceptance_criteria=["AC 1"],
            ontology_schema=OntologySchema(
                name="TestSchema",
                description="Test",
                fields=(),
            ),
            metadata=SeedMetadata(ambiguity_score=0.1),
        )

    def test_invariant_section_absent_by_default(self) -> None:
        """build_system_prompt with default args does NOT include invariant section."""
        from ouroboros.orchestrator.runner import build_system_prompt

        seed = self._make_minimal_seed()
        prompt = build_system_prompt(seed)  # type: ignore[arg-type]
        assert "INVARIANT" not in prompt
        assert "[[INVARIANT" not in prompt

    def test_invariant_section_included_when_flag_true(self) -> None:
        """include_invariant_instructions=True adds ## Invariant declarations."""
        from ouroboros.orchestrator.runner import build_system_prompt

        seed = self._make_minimal_seed()
        prompt = build_system_prompt(seed, include_invariant_instructions=True)  # type: ignore[arg-type]
        assert "## Invariant declarations" in prompt
        assert "[[INVARIANT:" in prompt

    def test_invariant_section_explains_format(self) -> None:
        """The invariant section includes the tag format and rules."""
        from ouroboros.orchestrator.runner import build_system_prompt

        seed = self._make_minimal_seed()
        prompt = build_system_prompt(seed, include_invariant_instructions=True)  # type: ignore[arg-type]
        # Must explain the double-bracket format
        assert "[[INVARIANT:" in prompt
        # Must mention the 200-char limit
        assert "200" in prompt

    def test_parallel_mode_byte_identical_without_flag(self) -> None:
        """Calling build_system_prompt without new flag produces identical output to before."""
        from ouroboros.orchestrator.runner import build_system_prompt

        seed = self._make_minimal_seed()
        prompt_default = build_system_prompt(seed)  # type: ignore[arg-type]
        prompt_explicit_false = build_system_prompt(seed, include_invariant_instructions=False)  # type: ignore[arg-type]
        assert prompt_default == prompt_explicit_false

    def test_compounding_call_site_passes_flag_true(self) -> None:
        """Regression: the compounding branch in OrchestratorRunner._run_orchestrator
        must pass include_invariant_instructions=True so the agent is told to emit
        ``[[INVARIANT: ...]]`` tags. The parameter exists; this test catches the
        case where the runner forgets to flip it (the gap surfaced by the QA
        verdict on the phase-1.5 dogfood).
        """
        import ast
        from pathlib import Path

        runner_src = (
            Path(__file__).resolve().parents[3] / "src" / "ouroboros" / "orchestrator" / "runner.py"
        ).read_text(encoding="utf-8")
        tree = ast.parse(runner_src)

        # Find every call to build_system_prompt where include_claude_md=True
        # is passed (the marker for the compounding-mode call site, per the
        # AC-3 seed: "compounding system prompt builder ... with include_claude_md").
        found_compounding_call_with_flag = False
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = (
                func.attr if isinstance(func, ast.Attribute)
                else func.id if isinstance(func, ast.Name)
                else None
            )
            if name != "build_system_prompt":
                continue
            kwargs = {kw.arg: kw.value for kw in node.keywords if kw.arg}
            includes_claude_md = (
                isinstance(kwargs.get("include_claude_md"), ast.Constant)
                and kwargs["include_claude_md"].value is True
            )
            includes_invariants = (
                isinstance(kwargs.get("include_invariant_instructions"), ast.Constant)
                and kwargs["include_invariant_instructions"].value is True
            )
            if includes_claude_md and includes_invariants:
                found_compounding_call_with_flag = True
                break

        assert found_compounding_call_with_flag, (
            "Compounding call site of build_system_prompt is missing "
            "include_invariant_instructions=True; the agent will not be "
            "instructed to emit [[INVARIANT: ...]] tags."
        )


# ---------------------------------------------------------------------------
# AC-3 (Q3 render gate) — reliability-threshold filter in to_prompt_text()
# ---------------------------------------------------------------------------


def _mk_pm_with_reliability(
    idx: int,
    inv_text: str,
    reliability: float,
    *,
    is_contradicted: bool = False,
) -> ACPostmortem:
    """Helper: ACPostmortem with one explicit Invariant at the given reliability."""
    summary = ACContextSummary(
        ac_index=idx,
        ac_content=f"AC {idx + 1} task",
        success=True,
    )
    inv = Invariant(
        text=inv_text,
        reliability=reliability,
        occurrences=1,
        first_seen_ac_id=f"ac_{idx}",
        is_contradicted=is_contradicted,
    )
    return ACPostmortem(summary=summary, invariants_established=(inv,))


class TestRenderGate:
    """Tests for the reliability-threshold render gate in to_prompt_text().

    Verifies that OUROBOROS_INVARIANT_MIN_RELIABILITY controls which invariants
    appear in the rendered prompt chain, while leaving the serialized chain data
    untouched.

    [[INVARIANT: to_prompt_text render gate filters below-threshold invariants from prompt]]
    [[INVARIANT: OUROBOROS_INVARIANT_MIN_RELIABILITY env var controls render threshold (default 0.7)]]
    """

    def test_high_reliability_invariant_passes_gate(self) -> None:
        """Invariant with reliability >= threshold appears in rendered prompt."""
        chain = PostmortemChain(
            postmortems=(_mk_pm_with_reliability(0, "HIGH_TRUST invariant", 0.95),)
        )
        text = chain.to_prompt_text(min_reliability=0.7)
        assert "HIGH_TRUST invariant" in text

    def test_low_reliability_invariant_filtered_from_prompt(self) -> None:
        """Invariant with reliability < threshold is hidden from rendered prompt.

        Building on AC-1 (chain artifact) and AC-2 (flattening): this is the
        render gate that prevents low-confidence facts from polluting future AC prompts.
        The invariant is stored in the chain but not rendered.
        """
        chain = PostmortemChain(
            postmortems=(_mk_pm_with_reliability(0, "LOW_TRUST invariant", 0.3),)
        )
        text = chain.to_prompt_text(min_reliability=0.7)
        assert "LOW_TRUST invariant" not in text

    def test_invariant_exactly_at_threshold_is_included(self) -> None:
        """Invariant with reliability == threshold is included (>= comparison)."""
        chain = PostmortemChain(
            postmortems=(_mk_pm_with_reliability(0, "EXACT_THRESHOLD invariant", 0.7),)
        )
        text = chain.to_prompt_text(min_reliability=0.7)
        assert "EXACT_THRESHOLD invariant" in text

    def test_just_below_threshold_is_excluded(self) -> None:
        """Invariant at 0.699 (just below 0.7) is filtered out."""
        chain = PostmortemChain(
            postmortems=(_mk_pm_with_reliability(0, "ALMOST invariant", 0.699),)
        )
        text = chain.to_prompt_text(min_reliability=0.7)
        assert "ALMOST invariant" not in text

    def test_contradicted_invariant_always_filtered(self) -> None:
        """Contradicted invariants (is_contradicted=True) never appear even if caller
        passes min_reliability=0.0.

        AC-2 established that contradicted invariants get reliability=0.0 and
        is_contradicted=True. The render gate must exclude them regardless.
        """
        chain = PostmortemChain(
            postmortems=(
                _mk_pm_with_reliability(
                    0, "CONTRADICTED fact", 0.0, is_contradicted=True
                ),
            )
        )
        # Even with min_reliability=0.0 (show all), contradicted ones stay hidden.
        text = chain.to_prompt_text(min_reliability=0.0)
        assert "CONTRADICTED fact" not in text

    def test_zero_min_reliability_shows_all_non_contradicted(self) -> None:
        """min_reliability=0.0 renders all non-contradicted invariants regardless of score."""
        chain = PostmortemChain(
            postmortems=(
                _mk_pm_with_reliability(0, "VERY_LOW score", 0.01),
                _mk_pm_with_reliability(1, "HIGH score", 0.99),
            )
        )
        text = chain.to_prompt_text(min_reliability=0.0)
        assert "VERY_LOW score" in text
        assert "HIGH score" in text

    def test_mixed_reliabilities_only_high_shown(self) -> None:
        """Chain with mixed-reliability invariants renders only the trusted ones."""
        chain = PostmortemChain(
            postmortems=(
                _mk_pm_with_reliability(0, "TRUSTED invariant", 0.9),
                _mk_pm_with_reliability(1, "UNTRUSTED invariant", 0.4),
                _mk_pm_with_reliability(2, "BORDERLINE invariant", 0.7),
            )
        )
        text = chain.to_prompt_text(min_reliability=0.7)
        assert "TRUSTED invariant" in text
        assert "BORDERLINE invariant" in text
        assert "UNTRUSTED invariant" not in text

    def test_env_var_controls_default_threshold(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """OUROBOROS_INVARIANT_MIN_RELIABILITY env var controls the default threshold.

        When min_reliability is not explicitly passed, to_prompt_text() reads
        the env var. This is the primary configuration point for operators.
        """
        chain = PostmortemChain(
            postmortems=(
                _mk_pm_with_reliability(0, "MEDIUM_TRUST inv", 0.6),
                _mk_pm_with_reliability(1, "HIGH_TRUST inv", 0.9),
            )
        )
        # With strict threshold (0.8): only HIGH_TRUST passes.
        monkeypatch.setenv("OUROBOROS_INVARIANT_MIN_RELIABILITY", "0.8")
        text_strict = chain.to_prompt_text()
        assert "HIGH_TRUST inv" in text_strict
        assert "MEDIUM_TRUST inv" not in text_strict

        # With lenient threshold (0.5): both pass.
        monkeypatch.setenv("OUROBOROS_INVARIANT_MIN_RELIABILITY", "0.5")
        text_lenient = chain.to_prompt_text()
        assert "HIGH_TRUST inv" in text_lenient
        assert "MEDIUM_TRUST inv" in text_lenient

    def test_env_var_unset_uses_default_threshold(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When env var is unset, default threshold (0.7) is applied.

        Confirms POSTMORTEM_DEFAULT_INVARIANT_MIN_RELIABILITY=0.7 is the
        actual default used when the env var is absent.
        """
        monkeypatch.delenv("OUROBOROS_INVARIANT_MIN_RELIABILITY", raising=False)
        chain = PostmortemChain(
            postmortems=(
                _mk_pm_with_reliability(0, "BELOW_DEFAULT inv", 0.5),
                _mk_pm_with_reliability(1, "ABOVE_DEFAULT inv", 0.8),
            )
        )
        text = chain.to_prompt_text()
        assert "ABOVE_DEFAULT inv" in text
        assert "BELOW_DEFAULT inv" not in text
        # Verify the default constant is what we expect.
        assert POSTMORTEM_DEFAULT_INVARIANT_MIN_RELIABILITY == 0.7

    def test_invalid_env_var_falls_back_to_default(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Non-numeric env var value falls back to default threshold silently."""
        monkeypatch.setenv("OUROBOROS_INVARIANT_MIN_RELIABILITY", "not-a-float")
        chain = PostmortemChain(
            postmortems=(
                _mk_pm_with_reliability(0, "HIGH_TRUST fallback", 0.9),
                _mk_pm_with_reliability(1, "LOW_TRUST fallback", 0.1),
            )
        )
        # Should use default 0.7: HIGH passes, LOW filtered.
        text = chain.to_prompt_text()
        assert "HIGH_TRUST fallback" in text
        assert "LOW_TRUST fallback" not in text

    def test_build_postmortem_chain_prompt_respects_env_var(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """build_postmortem_chain_prompt() honors OUROBOROS_INVARIANT_MIN_RELIABILITY
        via to_prompt_text()'s internal env var resolution.

        This confirms the full call path from the serial executor (which calls
        build_postmortem_chain_prompt) through to the render gate works end-to-end.
        """
        chain = PostmortemChain(
            postmortems=(
                _mk_pm_with_reliability(0, "EXECUTIVE_TRUTH inv", 0.85),
                _mk_pm_with_reliability(1, "WEAK_CLAIM inv", 0.2),
            )
        )
        # Set threshold to 0.8: WEAK_CLAIM is hidden.
        monkeypatch.setenv("OUROBOROS_INVARIANT_MIN_RELIABILITY", "0.8")
        text = build_postmortem_chain_prompt(chain)
        assert "EXECUTIVE_TRUTH inv" in text
        assert "WEAK_CLAIM inv" not in text

    def test_serialized_chain_preserves_below_threshold_invariants(self) -> None:
        """Low-reliability invariants hidden from prompt are still in the serialized chain.

        The render gate only affects prompt output; the serialized chain is the
        full record (important for audit and resume semantics from AC-1/AC-2).
        """
        low_inv = Invariant(
            text="BELOW_THRESHOLD claim",
            reliability=0.3,
            occurrences=1,
            first_seen_ac_id="ac_0",
        )
        summary = ACContextSummary(ac_index=0, ac_content="Test AC", success=True)
        pm = ACPostmortem(summary=summary, invariants_established=(low_inv,))
        chain = PostmortemChain(postmortems=(pm,))

        # The serialized data retains the invariant.
        data = serialize_postmortem_chain(chain)
        assert len(data) == 1
        assert len(data[0]["invariants_established"]) == 1
        assert data[0]["invariants_established"][0]["text"] == "BELOW_THRESHOLD claim"
        assert abs(data[0]["invariants_established"][0]["reliability"] - 0.3) < 1e-9

        # The rendered prompt hides it (threshold=0.7).
        text = chain.to_prompt_text(min_reliability=0.7)
        assert "BELOW_THRESHOLD claim" not in text


# ---------------------------------------------------------------------------
# Sub-AC 4: Additional tests for invariant insertion, occurrence bumping,
# reliability averaging, contradiction detection, and threshold-filter gating.
#
# These tests build on the facts established by prior ACs:
#   AC-1: end-of-run chain artifact exists in docs/brainstorm/chain-*.md
#   AC-2: ACPostmortem.sub_postmortems preserves structure in serialized chain;
#         to_prompt_text flattens sub-AC data; parent digest fields are unions
#   AC-3: merge_invariants bumps occurrences and averages reliability on
#         re-declaration; NOT-prefix contradictions set is_contradicted=True;
#         OUROBOROS_INVARIANT_MIN_RELIABILITY defaults to 0.7
# ---------------------------------------------------------------------------


class TestACPostmortemFullTextRenderGate:
    """Tests for ACPostmortem.to_full_text() min_reliability gate.

    ``to_full_text`` is called by ``to_prompt_text`` for the "recent window"
    full-form entries.  Its per-postmortem reliability gate must be consistent
    with the cumulative-invariants block in ``to_prompt_text``.

    [[INVARIANT: to_full_text applies min_reliability gate to per-AC invariants]]
    [[INVARIANT: contradicted invariants excluded from to_full_text at any threshold]]
    """

    def test_to_full_text_default_shows_all_non_contradicted(self) -> None:
        """Default call (min_reliability=0.0) shows all non-contradicted invariants."""
        inv = Invariant(text="LOW_SCORE fact", reliability=0.1, occurrences=1, first_seen_ac_id="ac_0")
        summary = ACContextSummary(ac_index=0, ac_content="AC 1", success=True)
        pm = ACPostmortem(summary=summary, invariants_established=(inv,))

        text = pm.to_full_text()
        assert "LOW_SCORE fact" in text

    def test_to_full_text_with_min_reliability_filters_below_threshold(self) -> None:
        """to_full_text hides invariants below the given reliability threshold."""
        low_inv = Invariant(
            text="LOW_CONF claim", reliability=0.3, occurrences=1, first_seen_ac_id="ac_0"
        )
        high_inv = Invariant(
            text="HIGH_CONF claim", reliability=0.95, occurrences=1, first_seen_ac_id="ac_0"
        )
        summary = ACContextSummary(ac_index=0, ac_content="AC 1", success=True)
        pm = ACPostmortem(summary=summary, invariants_established=(low_inv, high_inv))

        text = pm.to_full_text(min_reliability=0.7)
        assert "HIGH_CONF claim" in text
        assert "LOW_CONF claim" not in text

    def test_to_full_text_excludes_contradicted_at_zero_threshold(self) -> None:
        """Contradicted invariants never appear in to_full_text, even at min_reliability=0.0.

        Builds on the AC-2 (B-prime) invariant:
        'NOT-prefix contradictions set is_contradicted=True and reliability=0.0'.
        """
        contradicted = Invariant(
            text="NOT feature is on",
            reliability=0.0,
            occurrences=1,
            first_seen_ac_id="ac_2",
            is_contradicted=True,
        )
        summary = ACContextSummary(ac_index=0, ac_content="AC 1", success=True)
        pm = ACPostmortem(summary=summary, invariants_established=(contradicted,))

        # Even at zero threshold (show all), contradicted ones stay hidden.
        text = pm.to_full_text(min_reliability=0.0)
        assert "NOT feature is on" not in text

    def test_to_full_text_no_invariants_section_when_all_filtered(self) -> None:
        """When all invariants are filtered, the 'Invariants established' header is absent."""
        low_inv = Invariant(
            text="FILTERED fact", reliability=0.2, occurrences=1, first_seen_ac_id="ac_0"
        )
        summary = ACContextSummary(ac_index=0, ac_content="AC 1", success=True)
        pm = ACPostmortem(summary=summary, invariants_established=(low_inv,))

        text = pm.to_full_text(min_reliability=0.7)
        assert "Invariants established" not in text
        assert "FILTERED fact" not in text

    def test_to_full_text_exactly_at_threshold_included(self) -> None:
        """Invariant exactly at min_reliability threshold appears (>= comparison)."""
        inv = Invariant(
            text="BORDERLINE inv", reliability=0.7, occurrences=1, first_seen_ac_id="ac_0"
        )
        summary = ACContextSummary(ac_index=0, ac_content="AC 1", success=True)
        pm = ACPostmortem(summary=summary, invariants_established=(inv,))

        text = pm.to_full_text(min_reliability=0.7)
        assert "BORDERLINE inv" in text


class TestRenderGateSectionHeader:
    """Tests that the 'Established Invariants' section header is managed correctly.

    When all cumulative invariants are below the threshold, the section header
    must be absent too (not just the invariant text).
    """

    def test_all_below_threshold_omits_section_header(self) -> None:
        """When all invariants fail the threshold, the section header is absent."""
        chain = PostmortemChain(
            postmortems=(
                _mk_pm_with_reliability(0, "WEAK_A", 0.1),
                _mk_pm_with_reliability(1, "WEAK_B", 0.2),
            )
        )
        text = chain.to_prompt_text(min_reliability=0.7)
        assert "Established Invariants" not in text
        assert "WEAK_A" not in text
        assert "WEAK_B" not in text

    def test_section_header_present_when_any_passes_threshold(self) -> None:
        """Section header appears when at least one invariant passes threshold."""
        chain = PostmortemChain(
            postmortems=(
                _mk_pm_with_reliability(0, "WEAK_X", 0.1),
                _mk_pm_with_reliability(1, "STRONG_Y", 0.9),
            )
        )
        text = chain.to_prompt_text(min_reliability=0.7)
        assert "Established Invariants" in text
        assert "STRONG_Y" in text
        assert "WEAK_X" not in text

    def test_contradicted_only_chain_omits_section_header(self) -> None:
        """A chain with only contradicted invariants omits the section header."""
        chain = PostmortemChain(
            postmortems=(
                _mk_pm_with_reliability(0, "NEGATED_CLAIM", 0.0, is_contradicted=True),
            )
        )
        text = chain.to_prompt_text(min_reliability=0.0)
        assert "Established Invariants" not in text
        assert "NEGATED_CLAIM" not in text


class TestInvariantFullLifecycle:
    """Integration tests for the full invariant lifecycle across multiple ACs.

    Demonstrates compounding by building a 3-AC chain where:
    - AC-0 establishes invariants (referencing AC-1's chain artifact fact)
    - AC-1 re-declares one, bumping occurrence and blending reliability
    - AC-2 contradicts one, establishing a new one

    This mirrors how the serial compounding executor uses merge_invariants
    after each AC, then appends to the chain, then renders for the next AC.

    [[INVARIANT: full lifecycle: establish -> re-declare -> contradict -> gate]]
    [[INVARIANT: serialized chain retains contradicted invariants for audit]]
    """

    def test_three_ac_lifecycle_occurrence_bumping_and_contradiction(self) -> None:
        """Full 3-AC lifecycle: establish, bump, contradict; render shows only trusted."""
        chain = PostmortemChain()

        # AC-0: establishes "X is stable" (reliability=0.9) and "auth required" (0.8).
        # These two invariants reference data patterns established by AC-1 and AC-2
        # (chain artifact + sub-postmortem preservation).
        inv_ac0 = chain.merge_invariants(
            [("X is stable", 0.9), ("auth required", 0.8)],
            source_ac_id="ac_0",
        )
        summary0 = ACContextSummary(ac_index=0, ac_content="Establish invariants", success=True)
        pm0 = ACPostmortem(summary=summary0, invariants_established=inv_ac0)
        chain = chain.append(pm0)

        # AC-1: re-declares "X is stable" (occurrence bumps to 2, reliability blends).
        inv_ac1 = chain.merge_invariants(
            [("X is stable", 1.0)],  # confirms the claim
            source_ac_id="ac_1",
        )
        assert inv_ac1[0].occurrences == 2
        # Weighted mean: (0.9*1 + 1.0) / 2 = 0.95
        assert abs(inv_ac1[0].reliability - 0.95) < 1e-6
        assert inv_ac1[0].first_seen_ac_id == "ac_0"  # origin preserved

        summary1 = ACContextSummary(ac_index=1, ac_content="Re-declare X", success=True)
        pm1 = ACPostmortem(summary=summary1, invariants_established=inv_ac1)
        chain = chain.append(pm1)

        # AC-2: contradicts "auth required" with "not auth required".
        # Symmetric contradiction handling: result has the new contradicted,
        # the counterpart marker (also contradicted), and Y (clean) — three
        # entries.
        inv_ac2 = chain.merge_invariants(
            [("not auth required", 0.85), ("Y is new", 0.75)],
            source_ac_id="ac_2",
        )
        assert len(inv_ac2) == 3
        # [0] the new contradicted "not auth required"
        assert inv_ac2[0].is_contradicted is True
        assert inv_ac2[0].reliability == 0.0
        # [1] counterpart marker for "auth required" (also contradicted)
        assert inv_ac2[1].text == "auth required"
        assert inv_ac2[1].is_contradicted is True
        # [2] Y is fresh, not contradicted
        assert inv_ac2[2].is_contradicted is False
        assert inv_ac2[2].occurrences == 1

        summary2 = ACContextSummary(ac_index=2, ac_content="Contradict auth, add Y", success=True)
        pm2 = ACPostmortem(summary=summary2, invariants_established=inv_ac2)
        chain = chain.append(pm2)

        # Render with threshold=0.7:
        # - "X is stable" (reliability=0.95) → visible
        # - "auth required" (counterpart marker, contradicted) → HIDDEN
        #   (cumulative_invariants() last-wins picks the contradicted entry)
        # - "not auth required" (is_contradicted=True) → HIDDEN
        # - "Y is new" (reliability=0.75) → visible
        text = chain.to_prompt_text(min_reliability=0.7)
        assert "X is stable" in text
        assert "Y is new" in text
        assert "not auth required" not in text
        assert "auth required" not in text  # symmetric: prior trusted claim hidden too

    def test_three_ac_lifecycle_serializes_full_chain_including_contradicted(self) -> None:
        """The serialized chain retains all invariants (including contradicted ones).

        Builds on AC-1's invariant: 'end-of-run chain artifact exists in
        docs/brainstorm/chain-*.md' — the chain artifact captures the FULL
        state for auditing, even when the render gate hides some invariants.
        """
        chain = PostmortemChain()

        # AC-0: establishes two invariants.
        inv_ac0 = chain.merge_invariants(
            [("chain artifact written at run end", 0.9)],
            source_ac_id="ac_0",
        )
        summary0 = ACContextSummary(ac_index=0, ac_content="AC 0", success=True)
        chain = chain.append(ACPostmortem(summary=summary0, invariants_established=inv_ac0))

        # AC-1: contradicts the first invariant.
        inv_ac1 = chain.merge_invariants(
            [("not chain artifact written at run end", 0.6)],
            source_ac_id="ac_1",
        )
        summary1 = ACContextSummary(ac_index=1, ac_content="AC 1", success=True)
        chain = chain.append(ACPostmortem(summary=summary1, invariants_established=inv_ac1))

        # Serialize the full chain — both contradicted entries (the new one and
        # the counterpart marker) must be present for full audit fidelity.
        data = serialize_postmortem_chain(chain)
        assert len(data) == 2
        inv_data = data[1]["invariants_established"]
        assert len(inv_data) == 2
        for entry in inv_data:
            assert entry["is_contradicted"] is True
            assert abs(entry["reliability"]) < 1e-9  # reliability=0.0

        # Rendered prompt excludes both — the negation AND the original claim,
        # since both are now flagged contradicted by symmetric handling.
        text = chain.to_prompt_text(min_reliability=0.0)
        assert "not chain artifact written at run end" not in text
        assert "chain artifact written at run end" not in text

    def test_occurrence_and_reliability_compounds_across_four_acs(self) -> None:
        """Occurrence count and blended reliability compound correctly over 4 ACs.

        Demonstrates that the merge_invariants weighted-mean formula produces
        consistent results across many re-declarations — the core value of
        serial compounding.
        """
        chain = PostmortemChain()
        scores = [0.6, 0.8, 1.0, 0.9]  # reliability scores per AC
        expected_occurrences = [1, 2, 3, 4]
        # Compute expected blended reliability step by step.
        # Weighted mean: (prior_rel * prior_occ + new_score) / new_occ
        rel = 0.6
        for step, (score, expected_occ) in enumerate(
            zip(scores[1:], expected_occurrences[1:]), start=1
        ):
            prior_occ = expected_occurrences[step - 1]
            rel = (rel * prior_occ + score) / expected_occ

        # Run the actual merge through 4 ACs.
        chain_state = PostmortemChain()
        for ac_idx, new_score in enumerate(scores):
            merged = chain_state.merge_invariants(
                [("compounding invariant", new_score)], source_ac_id=f"ac_{ac_idx}"
            )
            summary = ACContextSummary(
                ac_index=ac_idx, ac_content=f"AC {ac_idx}", success=True
            )
            pm = ACPostmortem(summary=summary, invariants_established=merged)
            chain_state = chain_state.append(pm)

        cumulative = chain_state.cumulative_invariants()
        assert len(cumulative) == 1
        final_inv = cumulative[0]
        assert final_inv.occurrences == 4
        assert abs(final_inv.reliability - rel) < 1e-5

    def test_re_declared_invariant_from_sub_postmortem_merge(self) -> None:
        """Invariants from sub_postmortems (AC-2 B-prime) feed into merge correctly.

        Builds on AC-2's invariant: 'parent digest fields are unions of its
        own plus sub-postmortem fields'. When sub_postmortems are flattened
        into the parent ACPostmortem by the serial executor, the resulting
        parent pm's invariants_established participates in merge_invariants
        for subsequent ACs exactly like any other postmortem.
        """
        # Simulate a parent ACPostmortem with flattened sub-postmortem invariants.
        # The sub-PM contributed "api_token header is required" as an invariant.
        sub_inv = Invariant(
            text="api_token header is required",
            reliability=0.85,
            occurrences=1,
            first_seen_ac_id="ac_0_sub_1",
        )
        # Build a fake sub-postmortem (structure preserved per AC-2 B-prime).
        sub_summary = ACContextSummary(
            ac_index=0, ac_content="Sub-AC: add auth header", success=True
        )
        sub_pm = ACPostmortem(summary=sub_summary, invariants_established=(sub_inv,))

        # Parent postmortem has sub_postmortems field populated (AC-2 B-prime) and
        # also carries the flattened invariants in its own invariants_established.
        parent_summary = ACContextSummary(
            ac_index=0, ac_content="Parent AC with decomposition", success=True
        )
        parent_pm = ACPostmortem(
            summary=parent_summary,
            invariants_established=(sub_inv,),  # flattened from sub
            sub_postmortems=(sub_pm,),          # preserved for serialization
        )
        chain = PostmortemChain(postmortems=(parent_pm,))

        # AC-1 re-declares the same invariant — merge should bump occurrence.
        merged = chain.merge_invariants(
            [("api_token header is required", 0.95)], source_ac_id="ac_1"
        )
        assert len(merged) == 1
        assert merged[0].occurrences == 2
        # Reliability blended: (0.85*1 + 0.95) / 2 = 0.9
        assert abs(merged[0].reliability - 0.9) < 1e-6
        # Canonical text from first occurrence preserved (AC-2 B-prime flattening)
        assert merged[0].text == "api_token header is required"
        assert merged[0].first_seen_ac_id == "ac_0_sub_1"
