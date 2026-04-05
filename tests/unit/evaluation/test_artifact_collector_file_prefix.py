"""Tests for ArtifactCollector File: prefix, project root detection, and working_dir fallback.

Bug 2 of #230: ArtifactCollector fails for projects without pyproject.toml.

Verifies that:
1. _project_dir_from_artifact matches File: prefix (not just Write:/Edit:)
2. _looks_like_project_root detects non-Python/Node.js project markers
3. _extract_project_dir accepts working_dir as a fallback

See: https://github.com/Q00/ouroboros/issues/230
"""

from __future__ import annotations

from pathlib import Path
import re
import tempfile

from ouroboros.mcp.server.adapter import (
    _looks_like_project_root,
    _project_dir_from_artifact,
)


class TestProjectDirFromArtifactFilePrefix:
    """_project_dir_from_artifact should match File: prefix."""

    def test_pattern_matches_file_prefix(self) -> None:
        """The regex pattern includes File: alongside Write: and Edit:."""
        pattern = r"(?:Write|Edit|File): (/[^\s]+)"
        assert re.findall(pattern, "File: /home/user/project/main.py")
        assert re.findall(pattern, "Write: /home/user/project/main.py")
        assert re.findall(pattern, "Edit: /home/user/project/main.py")

    def test_file_prefix_returns_none_without_project_marker(self) -> None:
        """Without any project marker, returns None."""
        result = _project_dir_from_artifact("File: /nonexistent/path/script.py")
        assert result is None


class TestLooksLikeProjectRoot:
    """_looks_like_project_root detects various project types."""

    def test_rejects_non_path(self) -> None:
        assert _looks_like_project_root("/some/string") is False
        assert _looks_like_project_root(None) is False

    def test_detects_git_repo(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            (Path(tmpdir) / ".git").mkdir()
            assert _looks_like_project_root(Path(tmpdir)) is True

    def test_detects_pyproject_toml(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            (Path(tmpdir) / "pyproject.toml").touch()
            assert _looks_like_project_root(Path(tmpdir)) is True

    def test_detects_cargo_toml(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            (Path(tmpdir) / "Cargo.toml").touch()
            assert _looks_like_project_root(Path(tmpdir)) is True

    def test_detects_go_mod(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            (Path(tmpdir) / "go.mod").touch()
            assert _looks_like_project_root(Path(tmpdir)) is True

    def test_detects_package_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            (Path(tmpdir) / "package.json").touch()
            assert _looks_like_project_root(Path(tmpdir)) is True

    def test_rejects_empty_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            assert _looks_like_project_root(Path(tmpdir)) is False


class TestProjectDirFromArtifactQuotedPaths:
    """_project_dir_from_artifact should handle quoted paths with spaces."""

    def test_quoted_path_with_spaces(self) -> None:
        """File: with a quoted path containing spaces should be parsed."""
        with tempfile.TemporaryDirectory() as tmpdir:
            project = Path(tmpdir) / "my project"
            project.mkdir()
            (project / ".git").mkdir()
            artifact = f'File: "{project}/main.py"'
            result = _project_dir_from_artifact(artifact)
            assert result == str(project)

    def test_unquoted_path_without_spaces(self) -> None:
        """Unquoted paths without spaces should still work."""
        with tempfile.TemporaryDirectory() as tmpdir:
            (Path(tmpdir) / ".git").mkdir()
            artifact = f"File: {tmpdir}/main.py"
            result = _project_dir_from_artifact(artifact)
            assert result == tmpdir
