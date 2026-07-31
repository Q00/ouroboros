"""Unit tests for ouroboros.bigbang.explore.detect_brownfield."""

from pathlib import Path

import pytest

from ouroboros.bigbang.explore import detect_brownfield


class TestDetectBrownfield:
    """Test detect_brownfield function."""

    def test_detect_brownfield_with_config_file(self, tmp_path: Path) -> None:
        """Returns True when a recognised config file exists."""
        (tmp_path / "pyproject.toml").write_text("[project]\nname = 'demo'\n")
        assert detect_brownfield(tmp_path) is True

    def test_detect_brownfield_empty_dir(self, tmp_path: Path) -> None:
        """Returns False for an empty directory."""
        assert detect_brownfield(tmp_path) is False

    def test_detect_brownfield_nonexistent_path(self, tmp_path: Path) -> None:
        """Returns False for a path that does not exist."""
        assert detect_brownfield(tmp_path / "nonexistent") is False

    def test_detect_brownfield_with_package_json(self, tmp_path: Path) -> None:
        """Returns True for a JavaScript/TypeScript project."""
        (tmp_path / "package.json").write_text('{"name": "demo"}')
        assert detect_brownfield(tmp_path) is True

    def test_detect_brownfield_with_go_mod(self, tmp_path: Path) -> None:
        """Returns True for a Go project."""
        (tmp_path / "go.mod").write_text("module example.com/demo\n")
        assert detect_brownfield(tmp_path) is True

    def test_detect_brownfield_string_path(self, tmp_path: Path) -> None:
        """Accepts string paths in addition to Path objects."""
        (tmp_path / "Cargo.toml").write_text("[package]\nname = 'demo'\n")
        assert detect_brownfield(str(tmp_path)) is True

    def test_detect_brownfield_git_repository_without_manifest(self, tmp_path: Path) -> None:
        git_dir = tmp_path / ".git"
        git_dir.mkdir()
        (git_dir / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")

        assert detect_brownfield(tmp_path) is True

    def test_detect_brownfield_linked_worktree_without_manifest(self, tmp_path: Path) -> None:
        git_dir = tmp_path / "git-metadata"
        git_dir.mkdir()
        (git_dir / "HEAD").write_text("ref: refs/heads/worktree\n", encoding="utf-8")
        worktree = tmp_path / "linked"
        worktree.mkdir()
        (worktree / ".git").write_text(f"gitdir: {git_dir}\n", encoding="utf-8")

        assert detect_brownfield(worktree) is True

    def test_detect_brownfield_rejects_malformed_git_pointer(self, tmp_path: Path) -> None:
        (tmp_path / ".git").write_text("not-a-gitdir\n", encoding="utf-8")

        assert detect_brownfield(tmp_path) is False

    def test_detect_brownfield_rejects_empty_git_pointer(self, tmp_path: Path) -> None:
        (tmp_path / ".git").write_text("gitdir:\n", encoding="utf-8")
        (tmp_path / "HEAD").write_text("ref: refs/heads/not-git\n", encoding="utf-8")

        assert detect_brownfield(tmp_path) is False

    def test_detect_brownfield_rejects_multiline_git_pointer(self, tmp_path: Path) -> None:
        git_dir = tmp_path / "git-metadata"
        git_dir.mkdir()
        (git_dir / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
        (tmp_path / ".git").write_text(
            f"gitdir: {git_dir}\nunexpected\n",
            encoding="utf-8",
        )

        assert detect_brownfield(tmp_path) is False

    def test_detect_brownfield_rejects_nul_git_pointer(self, tmp_path: Path) -> None:
        (tmp_path / ".git").write_bytes(b"gitdir: bad\x00path\n")

        assert detect_brownfield(tmp_path) is False

    def test_detect_brownfield_rejects_wrong_case_git_pointer(self, tmp_path: Path) -> None:
        git_dir = tmp_path / "git-metadata"
        git_dir.mkdir()
        (git_dir / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
        (tmp_path / ".git").write_text(f"GITDIR: {git_dir}\n", encoding="utf-8")

        assert detect_brownfield(tmp_path) is False

    @pytest.mark.parametrize(
        "pointer_template",
        (
            "gitdir:{git_dir}\n",
            "gitdir:  {git_dir}\n",
            "gitdir:\t{git_dir}\n",
            "gitdir: {git_dir} \n",
            " gitdir: {git_dir}\n",
            "\ngitdir: {git_dir}\n",
        ),
    )
    def test_detect_brownfield_rejects_noncanonical_git_pointer_spacing(
        self,
        tmp_path: Path,
        pointer_template: str,
    ) -> None:
        git_dir = tmp_path / "git-metadata"
        git_dir.mkdir()
        (git_dir / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
        (tmp_path / ".git").write_text(
            pointer_template.format(git_dir=git_dir),
            encoding="utf-8",
        )

        assert detect_brownfield(tmp_path) is False

    @pytest.mark.parametrize("line_ending", ("", "\n", "\r\n", "\r"))
    def test_detect_brownfield_accepts_single_git_pointer_line_ending(
        self,
        tmp_path: Path,
        line_ending: str,
    ) -> None:
        git_dir = tmp_path / "git-metadata"
        git_dir.mkdir()
        (git_dir / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
        (tmp_path / ".git").write_bytes(f"gitdir: {git_dir}".encode() + line_ending.encode())

        assert detect_brownfield(tmp_path) is True
