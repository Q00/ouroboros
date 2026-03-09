"""Tests for git workflow detection from CLAUDE.md."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from ouroboros.core.git_workflow import (
    GitWorkflowConfig,
    detect_git_workflow,
    is_on_protected_branch,
)


class TestDetectGitWorkflow:
    """Test detect_git_workflow parsing."""

    def test_no_claude_md(self, tmp_path: Path) -> None:
        """Returns default config when no CLAUDE.md exists."""
        config = detect_git_workflow(tmp_path)

        assert config.use_branches is False
        assert config.auto_pr is False
        assert config.source == ""

    def test_pr_based_workflow(self, tmp_path: Path) -> None:
        """Detects 'PR-based workflow' pattern."""
        (tmp_path / "CLAUDE.md").write_text("Use PR-based workflow for all changes.")

        config = detect_git_workflow(tmp_path)

        assert config.use_branches is True
        assert config.source == str(tmp_path / "CLAUDE.md")

    def test_never_commit_to_main(self, tmp_path: Path) -> None:
        """Detects 'never commit directly to main' pattern."""
        (tmp_path / "CLAUDE.md").write_text("Never commit directly to main.")

        config = detect_git_workflow(tmp_path)

        assert config.use_branches is True
        assert "main" in config.protected_branches

    def test_never_push_to_master(self, tmp_path: Path) -> None:
        """Detects 'never push to master' pattern."""
        (tmp_path / "CLAUDE.md").write_text("Don't push to master without a PR.")

        config = detect_git_workflow(tmp_path)

        assert (
            config.use_branches is False
        )  # "Don't push to master" doesn't match PR workflow patterns
        assert "master" in config.protected_branches

    def test_always_create_branch(self, tmp_path: Path) -> None:
        """Detects 'always create a branch' pattern."""
        (tmp_path / "CLAUDE.md").write_text("Always create a feature branch before coding.")

        config = detect_git_workflow(tmp_path)

        assert config.use_branches is True

    def test_feature_branch_workflow(self, tmp_path: Path) -> None:
        """Detects 'feature branch workflow' pattern."""
        (tmp_path / "CLAUDE.md").write_text("We use feature branch workflow.")

        config = detect_git_workflow(tmp_path)

        assert config.use_branches is True

    def test_auto_pr_detection(self, tmp_path: Path) -> None:
        """Detects auto-PR creation preference."""
        (tmp_path / "CLAUDE.md").write_text(
            "PR-based workflow. Automatically create a pull request after pushing."
        )

        config = detect_git_workflow(tmp_path)

        assert config.use_branches is True
        assert config.auto_pr is True

    def test_no_auto_pr_for_general_pr_mention(self, tmp_path: Path) -> None:
        """General 'create a PR' does not trigger auto_pr."""
        (tmp_path / "CLAUDE.md").write_text("PR-based workflow. Create a pull request.")

        config = detect_git_workflow(tmp_path)

        assert config.use_branches is True
        assert config.auto_pr is False

    def test_claude_subdir(self, tmp_path: Path) -> None:
        """Finds CLAUDE.md in .claude/ subdirectory."""
        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir()
        (claude_dir / "CLAUDE.md").write_text("PR-based workflow.")

        config = detect_git_workflow(tmp_path)

        assert config.use_branches is True
        assert ".claude/CLAUDE.md" in config.source

    def test_root_claude_md_takes_precedence(self, tmp_path: Path) -> None:
        """Root CLAUDE.md takes precedence over .claude/CLAUDE.md."""
        (tmp_path / "CLAUDE.md").write_text("No special workflow.")
        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir()
        (claude_dir / "CLAUDE.md").write_text("PR-based workflow.")

        config = detect_git_workflow(tmp_path)

        assert config.use_branches is False  # Root file has no PR pattern
        assert config.source == str(tmp_path / "CLAUDE.md")

    def test_no_workflow_content(self, tmp_path: Path) -> None:
        """CLAUDE.md without workflow patterns returns default."""
        (tmp_path / "CLAUDE.md").write_text("# Project\nThis is a Python project.")

        config = detect_git_workflow(tmp_path)

        assert config.use_branches is False
        assert config.auto_pr is False

    def test_default_protected_branches(self, tmp_path: Path) -> None:
        """Default protected branches when PR workflow detected without explicit branches."""
        (tmp_path / "CLAUDE.md").write_text("Always create a branch.")

        config = detect_git_workflow(tmp_path)

        assert "main" in config.protected_branches
        assert "master" in config.protected_branches


class TestIsOnProtectedBranch:
    """Test is_on_protected_branch."""

    def test_on_main(self, tmp_path: Path) -> None:
        """Returns True when on main branch."""
        config = GitWorkflowConfig(use_branches=True, protected_branches=("main", "master"))

        with patch("subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0
            mock_run.return_value.stdout = "main\n"

            result = is_on_protected_branch(tmp_path, config)

        assert result is True

    def test_on_feature_branch(self, tmp_path: Path) -> None:
        """Returns False when on a feature branch."""
        config = GitWorkflowConfig(use_branches=True, protected_branches=("main", "master"))

        with patch("subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0
            mock_run.return_value.stdout = "feature/my-feature\n"

            result = is_on_protected_branch(tmp_path, config)

        assert result is False

    def test_git_not_available(self, tmp_path: Path) -> None:
        """Returns False when git is not available."""
        config = GitWorkflowConfig(use_branches=True)

        with patch("subprocess.run", side_effect=FileNotFoundError):
            result = is_on_protected_branch(tmp_path, config)

        assert result is False
